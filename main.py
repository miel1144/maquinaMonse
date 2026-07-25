import asyncio
import random
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# --- BASE DE DATOS EN MEMORIA ---
# Definimos el estado inicial de las máquinas de la maquiladora
maquinas_db = {
    "prensa-corte-01": {
        "nombre": "Prensa de Corte Hidráulica",
        "area": "Corte",
        "estado": "NORMAL",
        "consumos_base": {"electrico_kwh": 45.0, "aire_psi": 80.0, "gas_m3": 0.0, "agua_lt": 0.0},
        "consumos_actuales": {"electrico_kwh": 45.0, "aire_psi": 80.0, "gas_m3": 0.0, "agua_lt": 0.0}
    },
    "linea-costura-05": {
        "nombre": "Estación de Costura Asientos",
        "area": "Costura",
        "estado": "NORMAL",
        "consumos_base": {"electrico_kwh": 12.0, "aire_psi": 10.0, "gas_m3": 0.0, "agua_lt": 0.0},
        "consumos_actuales": {"electrico_kwh": 12.0, "aire_psi": 10.0, "gas_m3": 0.0, "agua_lt": 0.0}
    },
    "compresor-central": {
        "nombre": "Compresor de Aire Principal",
        "area": "Servicios",
        "estado": "NORMAL",
        "consumos_base": {"electrico_kwh": 110.0, "aire_psi": 0.0, "gas_m3": 0.0, "agua_lt": 5.0},
        "consumos_actuales": {"electrico_kwh": 110.0, "aire_psi": 0.0, "gas_m3": 0.0, "agua_lt": 5.0}
    }
}

# --- MOTOR DE SIMULACIÓN ---
async def motor_de_caos():
    """Bucle infinito que altera los consumos cada 3 segundos"""
    while True:
        for _id, info in maquinas_db.items():
            base = info["consumos_base"]
            actual = info["consumos_actuales"]
            estado = info["estado"]
            
            # Generamos un ruido aleatorio entre -2% y +2% para simular realidad
            ruido = lambda valor: valor * random.uniform(-0.02, 0.02) if valor > 0 else 0.0

            for recurso in base:
                # 1. Comportamiento Normal (Base + Ruido)
                actual[recurso] = round(base[recurso] + ruido(base[recurso]), 2)
                
                # 2. Inyección de Anomalías
                if estado == "PICO_ELECTRICO" and recurso == "electrico_kwh":
                    actual[recurso] = round(base[recurso] * 3.5, 2) # Multiplica x3.5 el consumo
                    
                if estado == "FUGA_AIRE" and recurso == "aire_psi":
                    actual[recurso] = round(base[recurso] + 45.0, 2) # Pérdida constante de presión
                    
        await asyncio.sleep(3) # El tiempo avanza cada 3 segundos reales

# --- CONFIGURACIÓN DE FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Esto enciende el simulador en segundo plano al arrancar la API
    tarea_simulacion = asyncio.create_task(motor_de_caos())
    yield
    # Esto lo apaga limpiamente al detener el servidor
    tarea_simulacion.cancel()

app = FastAPI(
    title="Simulador de Telemetría Industrial - Monse",
    version="1.0.0",
    lifespan=lifespan
)

# --- ENDPOINTS REST ---

@app.get("/api/maquinas")
def listar_maquinas():
    """Endpoint para que 'Monse' consuma los datos en tiempo real"""
    return maquinas_db

class CaosRequest(BaseModel):
    estado: str # Valores esperados: "NORMAL", "PICO_ELECTRICO", "FUGA_AIRE"

@app.post("/api/maquinas/{maquina_id}/caos")
def inyectar_caos(maquina_id: str, payload: CaosRequest):
    """Endpoint para alterar el estado de una máquina a tu antojo"""
    if maquina_id not in maquinas_db:
        raise HTTPException(status_code=404, detail="Máquina no encontrada en Katzkin")
        
    estados_validos = ["NORMAL", "PICO_ELECTRICO", "FUGA_AIRE"]
    if payload.estado not in estados_validos:
        raise HTTPException(status_code=400, detail=f"Estado inválido. Elige entre: {estados_validos}")
        
    maquinas_db[maquina_id]["estado"] = payload.estado
    return {"mensaje": f"Máquina {maquina_id} ahora está en modo {payload.estado}"}

# ------------------------------------------APRENDIENDO FAST API
# from fastapi import FastAPI
# from pydantic import BaseModel

# class Item(BaseModel):
#     name: str
#     edad: int


# app = FastAPI()

# @app.get('/')
# async def reed_root():
#     return {"mesagge": "Hola jehe pruebas"}

# @app.post('/items')
# async def create_item(item: Item):
#     return {'item': item, 'name': item.name, 'edad': item.edad}