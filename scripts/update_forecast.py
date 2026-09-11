#!/usr/bin/env python3
"""Atualiza previsoes e risco local para Funchalinho, Almada.

Fontes:
- IPMA: avisos oficiais, previsao diaria local ate 5 dias e previsao de curto prazo.
- Open-Meteo/ECMWF IFS: apoio numerico ate 7 dias; nao substitui avisos IPMA.
- NOAA CPC/NCEI: contexto ENSO e previsao sazonal/teleconexoes.

Nota: ECMWF Open Data AWS nao e usado como se fosse uma previsao sazonal.
Os ficheiros AWS sao previsoes numericas de curto/medio prazo e requerem
processamento GRIB. Para sazonalidade, o relatorio referencia ECMWF/C3S e
NOAA CPC, sem converter sinais mensais em probabilidades locais de temporal.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

LAT = 38.641  # aproximacao ao Funchalinho; ajustar se houver coordenadas exatas
LON = -9.169
LOCATION_NAME = "Funchalinho, Almada"
TIMEOUT = 30
OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")

# O IPMA usa localidades administrativas/georreferenciadas. O script tenta
# descobrir automaticamente a localidade mais proxima; este valor e fallback.
IPMA_GLOBAL_ID_FALLBACK = "1110600"  # Almada; confirmar no endpoint de localidades

IPMA_WARNINGS_URL = "https://api.ipma.pt/open-data/forecast/warnings/warnings_www.json"
IPMA_DAILY_TEMPLATE = "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/{global_id}.json"
IPMA_HP_TEMPLATE = "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/hp-daily-forecast-day{id_day}.json"
IPMA_LOCATIONS_URL = "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# NOAA publico: contexto oficial ENSO. A estrutura pode mudar; a recolha falha
# silenciosamente e o relatorio marca a fonte como indisponivel.
NOAA_ENSO_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "funchalinho-weather-risk/1.0"})


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    radius = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "locations", "localidades"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def find_ipma_location() -> dict[str, Any]:
    """Procura a localidade IPMA mais proxima das coordenadas do Funchalinho."""
    try:
        payload = get_json(IPMA_LOCATIONS_URL)
        records = extract_records(payload)
        candidates = []
        for item in records:
            lat = item.get("latitude", item.get("lat"))
            lon = item.get("longitude", item.get("lon"))
            gid = item.get("globalIdLocal", item.get("global_id_local"))
            if lat is None or lon is None or gid is None:
                continue
            try:
                distance = haversine_km(LAT, LON, float(lat), float(lon))
            except (TypeError, ValueError):
                continue
            candidates.append((distance, item))
        if candidates:
            return {"status": "success", "record": min(candidates, key=lambda x: x[0])[1], "distance_km": min(candidates)[0]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "fallback", "record": {"globalIdLocal": IPMA_GLOBAL_ID_FALLBACK}, "distance_km": None}


def fetch_ipma_warnings() -> dict[str, Any]:
    """Recolhe os avisos oficiais IPMA, normalmente validos ate 3 dias."""
    try:
        payload = get_json(IPMA_WARNINGS_URL)
        records = extract_records(payload)
        # O esquema IPMA pode usar idAreaAviso, district ou local.
        relevant = []
        for item in records:
            text = json.dumps(item, ensure_ascii=False).lower()
            if any(term in text for term in ("setúbal", "setubal", "almada")):
                relevant.append(item)
        return {"source": "IPMA warnings", "status": "success", "all": records, "relevant": relevant}
    except Exception as exc:
        return {"source": "IPMA warnings", "status": "error", "error": str(exc), "all": [], "relevant": []}


def fetch_ipma_daily(global_id: str) -> dict[str, Any]:
    """Recolhe a previsao IPMA local ate 5 dias."""
    url = IPMA_DAILY_TEMPLATE.format(global_id=global_id)
    try:
        payload = get_json(url)
        return {"source": "IPMA daily", "status": "success", "url": url, "data": payload}
    except Exception as exc:
        return {"source": "IPMA daily", "status": "error", "url": url, "error": str(exc), "data": {}}


def fetch_ipma_hp_days() -> dict[str, Any]:
    """Recolhe as previsoes IPMA de curto prazo hp-daily-forecast-day{idDay}."""
    result = {}
    for day_id in range(1, 4):
        url = IPMA_HP_TEMPLATE.format(id_day=day_id)
        try:
            result[str(day_id)] = {"status": "success", "url": url, "data": get_json(url)}
        except Exception as exc:
            result[str(day_id)] = {"status": "error", "url": url, "error": str(exc), "data": {}}
    return {"source": "IPMA high priority daily", "status": "success", "days": result}


def fetch_open_meteo() -> dict[str, Any]:
    """Recolhe previsao de apoio baseada no ECMWF IFS/Open-Meteo."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": ",".join([
            "precipitation_sum", "precipitation_probability_max", "wind_speed_10m_max",
            "wind_gusts_10m_max", "temperature_2m_max", "temperature_2m_min",
            "pressure_msl_mean", "weather_code"
        ]),
        "hourly": "precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,pressure_msl",
        "forecast_days": 7,
        "timezone": "Europe/Lisbon",
        "models": "ecmwf_ifs025",
    }
    try:
        return {"source": "Open-Meteo ECMWF IFS", "status": "success", "data": get_json(OPEN_METEO_URL, params)}
    except Exception as exc:
        return {"source": "Open-Meteo ECMWF IFS", "status": "error", "error": str(exc), "data": {}}


def fetch_noaa_enso() -> dict[str, Any]:
    """Recolhe ONI da NOAA CPC como contexto climatico, nao como previsao local."""
    try:
        response = SESSION.get(NOAA_ENSO_URL, timeout=TIMEOUT)
        response.raise_for_status()
        lines = [line.strip() for line in response.text.splitlines() if line.strip() and not line.startswith("#")]
        return {"source": "NOAA CPC ONI", "status": "success", "url": NOAA_ENSO_URL, "latest_lines": lines[-8:]}
    except Exception as exc:
        return {"source": "NOAA CPC ONI", "status": "error", "url": NOAA_ENSO_URL, "error": str(exc), "latest_lines": []}


def flatten_daily(payload: Any) -> list[dict[str, Any]]:
    """Normaliza alguns esquemas comuns do IPMA para uma lista de dias."""
    records = extract_records(payload)
    if records:
        return records
    if isinstance(payload, dict):
        for key in ("forecast", "forecastData", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def number(item: dict[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        value = item.get(key)
        if value in (None, "", "null"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def calculate_risk(ipma_daily: dict[str, Any], warnings: dict[str, Any], open_meteo: dict[str, Any]) -> dict[str, Any]:
    """Calcula risco operacional conservador para os proximos 3 dias.

    Nao estima probabilidade de um desastre. Classifica a severidade prevista e
    a existencia de aviso oficial. O aviso IPMA prevalece sobre fontes auxiliares.
    """
    score = 0
    actions: set[str] = set()
    categories = {"wind": "LOW", "rain": "LOW", "coastal": "LOW", "temperature": "LOW"}

    warning_text = json.dumps(warnings.get("relevant", []), ensure_ascii=False).lower()
    if "vermelho" in warning_text or '"red"' in warning_text:
        score += 5
        actions.add("Seguir imediatamente as instrucoes da Protecao Civil")
    elif "laranja" in warning_text or '"orange"' in warning_text:
        score += 3
        actions.add("Preparar a habitacao e limitar deslocacoes")
    elif "amarelo" in warning_text or '"yellow"' in warning_text:
        score += 1
        actions.add("Monitorizar o aviso e a atualizacao seguinte do IPMA")

    for item in flatten_daily(ipma_daily.get("data", {}))[:5]:
        rain = number(item, "precipitaProb", "precipitationProbability", "precipitation", "precipitationSum")
        wind = number(item, "predWindSpeed", "windSpeed", "wind_speed")
        gust = number(item, "windGust", "wind_gusts", "gust")
        tmax = number(item, "tMax", "temperatureMax", "temp_max")

        if rain is not None and rain >= 60:
            categories["rain"] = "HIGH"
            score += 3
            actions.add("Verificar caleiras, sumidouros e drenagem")
        elif rain is not None and rain >= 30:
            categories["rain"] = "MEDIUM"
            score += 1
            actions.add("Monitorizar acumulacao de agua")
        if (gust is not None and gust >= 90) or (wind is not None and wind >= 70):
            categories["wind"] = "HIGH" if (gust or 0) >= 100 else "MEDIUM"
            score += 3 if categories["wind"] == "HIGH" else 1
            actions.add("Fixar objetos exteriores e verificar a cobertura")
        if tmax is not None and tmax >= 30:
            categories["temperat
