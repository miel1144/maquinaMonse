"""
MaquinaMonse - Microservicio FastAPI
CRUD de máquinas + autenticación simple para el enlace con Monse (Django)

Cómo correrlo en local:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8001

Docs automáticas (para probar sin construir un frontend todavía):
    http://localhost:8001/docs

IMPORTANTE — si vienes de una versión anterior de este archivo:
Cambiar los campos del modelo Maquina NO actualiza una tabla que ya
existe en maquinamonse.db (SQLModel solo CREA tablas nuevas, nunca
las altera). Borra el archivo maquinamonse.db antes de volver a
arrancar para que se regenere con el esquema correcto:
    rm maquinamonse.db      (Mac/Linux)
    del maquinamonse.db     (Windows)
"""
from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Header
from sqlmodel import SQLModel, Field, create_engine, Session, select
# Del simulador
import asyncio
from simulador import router as router_simulador, bucle_simulador

# ---------------------------------------------------------------------------
# 1. BASE DE DATOS
# ---------------------------------------------------------------------------
DATABASE_URL = "sqlite:///./maquinamonse.db"
engine = create_engine(DATABASE_URL, echo=False)



def crear_tablas():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# 2. MODELO / TABLA
# ---------------------------------------------------------------------------
# NOTA DE DISEÑO: id_medidor YA NO VIVE AQUÍ.
# La relación máquina↔medidor (N:M, con fecha de asignación, etc.) es
# conocimiento del dominio energético — vive completamente en Monse,
# vía la tabla maquinamedidor. MaquinaMonse no necesita saber cuántos
# ni cuáles medidores monitorean una máquina, solo en qué área está.
class Maquina(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    codigo_interno: str = Field(unique=True, index=True)
    nombre: str
    modelo: Optional[str] = None
    potencia_nominal: Optional[float] = None
    horas_diarias: Optional[float] = None
    estado: bool = Field(default=True)

    # Nace vacío en MaquinaMonse. Monse es quien lo llena y lo regresa
    # (endpoint PATCH abajo).
    area: Optional[str] = None

    # True cuando Monse ya nos confirmó el área asignada.
    sincronizada: bool = Field(default=False)

    creado_en: datetime = Field(default_factory=datetime.utcnow)
    actualizado_en: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 3. SCHEMAS DE ENTRADA (el "contrato")
# ---------------------------------------------------------------------------

class MaquinaCreate(SQLModel):
    """Lo que manda la interfaz de MaquinaMonse al dar de alta una máquina."""
    codigo_interno: str
    nombre: str
    modelo: Optional[str] = None
    potencia_nominal: Optional[float] = None
    horas_diarias: Optional[float] = None
    estado: bool = True
    # área NO se pide aquí a propósito: nace null siempre.


class MaquinaUpdateDesdeMonse(SQLModel):
    """
    Lo que Monse manda de regreso cuando ya asignó área.
    Todo opcional: es una actualización parcial, no hace falta
    mandar todos los campos cada vez.
    """
    area: Optional[str] = None
    estado: Optional[bool] = None
    horas_diarias: Optional[float] = None


# ---------------------------------------------------------------------------
# 4. SEGURIDAD ENTRE SERVICIOS (API key simple)
# ---------------------------------------------------------------------------
API_KEY_ESPERADA = "clave-secreta-monse-maquinamonse"  # luego -> variable de entorno


def verificar_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY_ESPERADA:
        raise HTTPException(status_code=401, detail="API key inválida")


# ---------------------------------------------------------------------------
# 5. CICLO DE VIDA
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Arranque y apagado de la aplicación.

    OJO: el bucle del simulador se lanza AQUÍ y no con @app.on_event("startup").
    Cuando se le pasa lifespan= al constructor de FastAPI, los manejadores
    registrados con on_event NO SE EJECUTAN NUNCA -- Starlette solo los corre
    cuando no hay un lifespan propio. Como esta app sí tiene uno (para
    crear_tablas), el on_event quedaba muerto sin ningún error visible: el
    endpoint respondía, pero los acumulados se quedaban congelados en su primer
    valor y Monse calculaba consumo = 0 para siempre.
    """
    crear_tablas()

    tarea = asyncio.create_task(bucle_simulador())
    try:
        yield
    finally:
        # Al apagar el servidor se cancela la tarea, para que uvicorn --reload
        # no vaya acumulando un simulador por cada recarga.
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass


app = FastAPI(title="MaquinaMonse", version="2.1.0", lifespan=lifespan)

app.include_router(router_simulador)

# ---------------------------------------------------------------------------
# 6. ENDPOINTS — el CRUD real
# ---------------------------------------------------------------------------

@app.post("/api/maquinas", response_model=Maquina, status_code=201)
def crear_maquina(datos: MaquinaCreate, session: Session = Depends(get_session)):
    """Se llama desde el formulario/interfaz de MaquinaMonse. area nace en None."""
    existe = session.exec(
        select(Maquina).where(Maquina.codigo_interno == datos.codigo_interno)
    ).first()
    if existe:
        raise HTTPException(400, "Ya existe una máquina con ese codigo_interno")

    maquina = Maquina(**datos.dict())
    session.add(maquina)
    session.commit()
    session.refresh(maquina)
    return maquina


@app.get("/api/maquinas", response_model=List[Maquina])
def listar_maquinas(session: Session = Depends(get_session)):
    """Monse llama aquí para jalar el catálogo completo (ver services.py de Django)."""
    return session.exec(select(Maquina)).all()


@app.get("/api/maquinas/{maquina_id}", response_model=Maquina)
def obtener_maquina(maquina_id: int, session: Session = Depends(get_session)):
    maquina = session.get(Maquina, maquina_id)
    if not maquina:
        raise HTTPException(404, "Máquina no encontrada")
    return maquina


@app.patch("/api/maquinas/por-codigo/{codigo_interno}", response_model=Maquina)
def actualizar_desde_monse(
    codigo_interno: str,
    datos: MaquinaUpdateDesdeMonse,
    session: Session = Depends(get_session),
    _=Depends(verificar_api_key),
):
    """
    Monse llama aquí cuando un usuario ya asignó área a la máquina.
    Ya NO se manda ni se guarda id_medidor: esa relación es exclusiva
    de Monse (maquinamedidor, N:M).
    """
    maquina = session.exec(
        select(Maquina).where(Maquina.codigo_interno == codigo_interno)
    ).first()
    if not maquina:
        raise HTTPException(404, "Máquina no encontrada")

    if datos.area is not None:
        maquina.area = datos.area
        maquina.sincronizada = True
    if datos.estado is not None:
        maquina.estado = datos.estado

    maquina.actualizado_en = datetime.utcnow()

    session.add(maquina)
    session.commit()
    session.refresh(maquina)
    return maquina