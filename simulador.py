"""
simulador.py — MaquinaMonse

Simulador de medidores + endpoint GET /api/maquinas/lecturas.

Está en un archivo aparte y no dentro de main.py a propósito: así se integra con
tres líneas y no hay que reescribir lo que ya tienes. Ver INTEGRACION al final.

QUÉ SIMULA

Un medidor real es un contador ACUMULADO: nunca baja, solo avanza. Por eso el
simulador guarda un acumulado por (máquina, tipo de energía) y en cada tick le
suma lo consumido en ese intervalo. Monse calcula el consumo del periodo como la
diferencia contra la lectura anterior, así que si el simulador devolviera un
valor instantáneo en vez de un acumulado, todos los consumos saldrían mal.
"""
import asyncio
import random
import time as time_module
from datetime import datetime, time, timezone

from fastapi import APIRouter

router = APIRouter()


# ===========================================================================
# CONFIGURACIÓN
# ===========================================================================

# Cada cuánto avanza el simulador. No es cada cuánto lo consulta Monse: son
# cosas distintas. El simulador puede latir cada 5 s y Monse leer cada 15 min;
# el acumulado ya trae todo lo ocurrido en medio.
INTERVALO_SEGUNDOS = 5

# Consumo nominal por hora de cada tipo de energía, por unidad de potencia de la
# máquina. Los números son plausibles, no exactos: la idea es que las gráficas
# del dashboard tengan forma realista.
PERFILES = {
    'ELEC': {'unidad': 'kWh', 'por_hora': 0.85},   # x potencia_nominal (kW)
    'AGUA': {'unidad': 'm3',  'por_hora': 0.012},
    'GAS':  {'unidad': 'm3',  'por_hora': 0.040},
}

# Factor de carga por turno. Una planta no consume igual a las 3 de la mañana
# que a media mañana; sin esto las gráficas salen planas y no se distingue un
# turno de otro.
#
# Los rangos coinciden con la tabla turno de Monse:
#   MAT 06:00-14:00 | VES 14:00-22:00 | NOC 22:00-06:00
CARGA_POR_TURNO = [
    (time(6, 0),  time(14, 0), 1.00),   # matutino: producción plena
    (time(14, 0), time(22, 0), 0.85),   # vespertino
    (time(22, 0), time(6, 0),  0.35),   # nocturno: guardia y mantenimiento
]

# Ruido aleatorio por tick (+/- 2%), para que dos máquinas iguales no den
# exactamente lo mismo.
RUIDO = 0.02


# ===========================================================================
# ESTADO EN MEMORIA
# ===========================================================================
# {(codigo_interno, cod_tipo_energia): acumulado}
_acumulados: dict[tuple[str, str], float] = {}
_ultimo_tick: datetime | None = None


def _factor_carga(momento: datetime) -> float:
    """Factor de carga según el turno en curso."""
    hora = momento.time()
    for inicio, fin, factor in CARGA_POR_TURNO:
        if inicio < fin:
            if inicio <= hora < fin:
                return factor
        else:
            # Turno que cruza la medianoche (22:00 -> 06:00). Sin este caso, de
            # las 22:00 a las 06:00 no coincidiría ningún rango y el factor
            # quedaría en el de respaldo, aplanando todo el turno nocturno.
            if hora >= inicio or hora < fin:
                return factor
    return 0.5


# Caché de la lista de máquinas. El bucle late cada 5 segundos y consultar
# SQLite en cada tick es innecesario: el catálogo casi no cambia.
_maquinas_cache: list[dict] = []
_maquinas_cache_en: float = 0.0
MAQUINAS_CACHE_SEGUNDOS = 60


def _maquinas_simuladas() -> list[dict]:
    """
    Lee las máquinas de la base de MaquinaMonse (SQLModel / maquinamonse.db).

    El import va DENTRO de la función a propósito. main.py hace
    `from simulador import router, bucle_simulador` en sus primeras líneas, así
    que si aquí importáramos main a nivel de módulo tendríamos un ciclo y
    ninguno de los dos podría cargar. Diferido funciona porque para cuando esta
    función se llama, main ya terminó de importarse.
    """
    global _maquinas_cache, _maquinas_cache_en

    ahora = time_module.monotonic()
    if _maquinas_cache and (ahora - _maquinas_cache_en) < MAQUINAS_CACHE_SEGUNDOS:
        return _maquinas_cache

    try:
        from sqlmodel import Session, select
        from main import Maquina, engine

        with Session(engine) as session:
            filas = session.exec(select(Maquina)).all()

        _maquinas_cache = [
            {
                'codigo_interno': f.codigo_interno,
                # potencia_nominal es Optional en tu modelo: si viene vacía se
                # usa un valor razonable en vez de simular consumo cero, que
                # haría parecer que la máquina está apagada.
                'potencia_nominal': float(f.potencia_nominal or 10.0),
                'estado': bool(f.estado),
            }
            for f in filas
        ]
        _maquinas_cache_en = ahora

    except Exception as e:
        print(f'[simulador] no se pudo leer el catálogo de máquinas: {e}')
        # Se conserva lo último que sí se pudo leer, para que un error puntual
        # de la base no congele la simulación.

    return _maquinas_cache


def avanzar_simulacion(segundos: float | None = None) -> None:
    """
    Hace avanzar los contadores. Es lo que llama la tarea de segundo plano.

    Se calcula con el tiempo REAL transcurrido y no con INTERVALO_SEGUNDOS fijo:
    si el proceso se pausa o el equipo se suspende, el acumulado sigue siendo
    coherente con el reloj en vez de quedarse corto.
    """
    global _ultimo_tick

    ahora = datetime.now(timezone.utc)
    if segundos is None:
        segundos = (ahora - _ultimo_tick).total_seconds() if _ultimo_tick else INTERVALO_SEGUNDOS
    _ultimo_tick = ahora

    horas = max(segundos, 0) / 3600.0
    carga = _factor_carga(datetime.now())

    for maquina in _maquinas_simuladas():
        codigo = maquina['codigo_interno']
        if not codigo or not maquina.get('estado', True):
            # Una máquina apagada no consume: su acumulado se queda quieto.
            continue

        potencia = maquina['potencia_nominal']

        for tipo, perfil in PERFILES.items():
            base = perfil['por_hora'] * potencia * horas * carga
            incremento = base * (1 + random.uniform(-RUIDO, RUIDO))

            clave = (codigo, tipo)
            _acumulados[clave] = round(_acumulados.get(clave, 0.0) + max(incremento, 0.0), 4)


async def bucle_simulador() -> None:
    """Tarea de segundo plano: avanza la simulación cada INTERVALO_SEGUNDOS."""
    while True:
        try:
            avanzar_simulacion()
        except Exception as e:
            # Un error aquí NO debe matar la tarea: si se cae, el simulador deja
            # de avanzar y el endpoint devolvería siempre lo mismo sin que nada
            # lo delate.
            print(f'[simulador] error en el tick: {e}')
        await asyncio.sleep(INTERVALO_SEGUNDOS)


# ===========================================================================
# ENDPOINT
# ===========================================================================
@router.get('/api/maquinas/lecturas')
def lecturas():
    """
    Lecturas acumuladas actuales de todas las máquinas.

    CONTRATO (lo que Monse espera; ver general/lecturas.py):

        {
          "generado_en": "2026-07-31T15:00:00+00:00",
          "lecturas": [
            {
              "codigo_interno": "MAQ-001",
              "cod_tipo_energia": "ELEC",
              "valor_acumulado": 1512.7500,
              "unidad": "kWh"
            }
          ]
        }

    cod_tipo_energia debe coincidir EXACTO con la tabla tipoenergia de Monse
    ('ELEC', 'AGUA', 'GAS'). Monse usa ese código para decidir a cuál de los
    medidores de la máquina corresponde la lectura; si no coincide, la lectura
    se descarta como "sin medidor asignado".
    """
    # Se avanza también al consultar, para que el primer GET no devuelva ceros
    # si la tarea de segundo plano todavía no ha dado su primer tick.
    if not _acumulados:
        avanzar_simulacion(INTERVALO_SEGUNDOS)

    salida = []
    for (codigo, tipo), valor in sorted(_acumulados.items()):
        salida.append({
            'codigo_interno': codigo,
            'cod_tipo_energia': tipo,
            'valor_acumulado': valor,
            'unidad': PERFILES[tipo]['unidad'],
        })

    return {
        'generado_en': datetime.now(timezone.utc).isoformat(),
        'lecturas': salida,
    }


# ===========================================================================
# INTEGRACIÓN EN main.py  (tres líneas)
# ===========================================================================
#
#   import asyncio
#   from simulador import router as router_simulador, bucle_simulador
#
#   app.include_router(router_simulador)
#
#   @app.on_event("startup")
#   async def _arrancar_simulador():
#       asyncio.create_task(bucle_simulador())
#
# Si tu main.py ya usa el ciclo de vida moderno (lifespan) en vez de
# on_event, la línea del create_task va dentro de ese lifespan.