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

def calculate_risk(
    ipma_daily: dict[str, Any],
    warnings: dict[str, Any],
    open_meteo: dict[str, Any],
) -> dict[str, Any]:
    """Calcula o risco operacional para os próximos três dias."""

    score = 0
    actions: set[str] = set()

    categories = {
        "wind": "LOW",
        "rain": "LOW",
        "coastal": "LOW",
        "temperature": "LOW",
    }

    warning_text = json.dumps(
        warnings.get("relevant", []),
        ensure_ascii=False,
    ).lower()

    if "vermelho" in warning_text or '"red"' in warning_text:
        score += 5
        actions.add(
            "Seguir imediatamente as instruções da Proteção Civil"
        )

    elif "laranja" in warning_text or '"orange"' in warning_text:
        score += 3
        actions.add(
            "Preparar a habitação e limitar deslocações"
        )

    elif "amarelo" in warning_text or '"yellow"' in warning_text:
        score += 1
        actions.add(
            "Monitorizar o aviso e a atualização seguinte do IPMA"
        )

    daily_records = flatten_daily(
        ipma_daily.get("data", {})
    )

    for item in daily_records[:5]:
        rain = number(
            item,
            "precipitaProb",
            "precipitationProbability",
            "precipitation",
            "precipitationSum",
        )

        wind = number(
            item,
            "predWindSpeed",
            "windSpeed",
            "wind_speed",
        )

        gust = number(
            item,
            "windGust",
            "wind_gusts",
            "gust",
        )

        tmax = number(
            item,
            "tMax",
            "temperatureMax",
            "temp_max",
        )

        if rain is not None and rain >= 60:
            categories["rain"] = "HIGH"
            score += 3
            actions.add(
                "Verificar caleiras, sumidouros e drenagem"
            )

        elif rain is not None and rain >= 30:
            if categories["rain"] != "HIGH":
                categories["rain"] = "MEDIUM"

            score += 1
            actions.add(
                "Monitorizar acumulação de água"
            )

        if (
            (gust is not None and gust >= 90)
            or (wind is not None and wind >= 70)
        ):
            if (
                gust is not None
                and gust >= 100
            ):
                categories["wind"] = "HIGH"
                score += 3
            else:
                if categories["wind"] != "HIGH":
                    categories["wind"] = "MEDIUM"

                score += 1

            actions.add(
                "Fixar objetos exteriores e verificar a cobertura"
            )

        if tmax is not None and tmax >= 30:
            categories["temperature"] = "MEDIUM"
            score += 1
            actions.add(
                "Manter hidratação e proteger pessoas vulneráveis do calor"
            )

    if (
        categories["rain"] != "LOW"
        or categories["wind"] != "LOW"
    ):
        categories["coastal"] = "MEDIUM"
        actions.add(
            "Evitar arribas, zonas expostas e acessos costeiros durante temporal"
        )

    if open_meteo.get("status") == "success":
        open_meteo_daily = (
            open_meteo
            .get("data", {})
            .get("daily", {})
        )

        for rain_value in open_meteo_daily.get(
            "precipitation_sum",
            [],
        )[:3]:
            if (
                isinstance(rain_value, (int, float))
                and rain_value >= 40
            ):
                score += 1
                actions.add(
                    "Confirmar a evolução da chuva nas atualizações IPMA"
                )
                break

    if score >= 8:
        overall = "HIGH"
    elif score >= 3:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    return {
        "overall": overall,
        "score": score,
        "categories": categories,
        "actions": sorted(actions),
    }


def generate_report(
    location: dict[str, Any],
    sources: dict[str, Any],
    risk: dict[str, Any],
    timestamp: str,
) -> str:
    record = location.get("record", {})
    location_id = record.get(
        "globalIdLocal",
        record.get("global_id_local", IPMA_GLOBAL_ID_FALLBACK),
    )

    distance = location.get("distance_km")
    if isinstance(distance, (int, float)):
        distance_text = f"{distance:.1f} km"
    else:
        distance_text = "nao determinada"

    lines = [
        f"# Relatorio meteorologico e de risco - {LOCATION_NAME}",
        "",
        f"**Atualizado:** {timestamp}",
        f"**Coordenadas:** {LAT}, {LON}",
        f"**Localidade IPMA:** {location_id}",
        f"**Distancia aproximada:** {distance_text}",
        "",
        "> Este relatorio apoia a preparacao local. "
        "Nao substitui os avisos oficiais do IPMA, da Protecao Civil ou o 112.",
        "",
        f"## Nivel operacional: {risk['overall']}",
        "",
        f"**Score:** {risk['score']}",
        "",
        "| Categoria | Nivel |",
        "|---|---|",
    ]

    for category, level in risk["categories"].items():
        lines.append(f"| {category.capitalize()} | {level} |")

    lines.extend(["", "## Acoes recomendadas", ""])

    if risk["actions"]:
        for action in risk["actions"]:
            lines.append(f"- [ ] {action}")
    else:
        lines.append("- Sem acoes adicionais identificadas.")

    daily_payload = sources.get("ipma_daily", {}).get("data", {})
    daily_records = flatten_daily(daily_payload)

    lines.extend(
        [
            "",
            "## Previsao diaria IPMA",
            "",
            "| Data | Temperatura maxima | Precipitacao/probabilidade | Vento |",
            "|---|---:|---:|---:|",
        ]
    )

    for item in daily_records[:5]:
        date_value = item.get(
            "forecastDate",
            item.get("date", item.get("time", "N/D")),
        )
        temperature = item.get(
            "tMax",
            item.get("temperatureMax", item.get("temp_max", "N/D")),
        )
        precipitation = item.get(
            "precipitaProb",
            item.get(
                "precipitationProbability",
                item.get("precipitation", "N/D"),
            ),
        )
        wind = item.get(
            "predWindSpeed",
            item.get("windSpeed", item.get("wind_speed", "N/D")),
        )

        lines.append(
            f"| {date_value} | {temperature} | "
            f"{precipitation} | {wind} |"
        )

    if not daily_records:
        lines.append("| Dados IPMA nao disponiveis | N/D | N/D | N/D |")

    lines.extend(["", "## Avisos IPMA", ""])

    warnings = sources.get("warnings", {}).get("relevant", [])

    if warnings:
        for warning in warnings:
            warning_text = json.dumps(
                warning,
                ensure_ascii=False,
            )
            lines.append(f"- `{warning_text}`")
    else:
        lines.append(
            "- Sem aviso relevante identificado para Almada/Setubal."
        )

    lines.extend(
        [
            "",
            "## Fontes",
            "",
            f"- IPMA avisos: {IPMA_WARNINGS_URL}",
            f"- IPMA previsao diaria: "
            f"{IPMA_DAILY_TEMPLATE.format(global_id=location_id)}",
            "- Open-Meteo com ECMWF IFS: previsao numerica auxiliar.",
            "- NOAA CPC ONI: contexto ENSO, nao previsao local.",
            "- ECMWF/Copernicus: referencia para previsao sazonal.",
            "",
            "## Limites",
            "",
            "O score e um indicador operacional de severidade prevista. "
            "Nao representa a probabilidade estatistica de ocorrencia de "
            "um desastre local.",
        ]
    )

    return "\n".join(lines) + "\n"


def main() -> None:
    timestamp = datetime.now().astimezone().strftime(
        "%Y-%m-%d %H:%M %Z"
    )

    print("A procurar a localidade IPMA mais proxima...")
    location = find_ipma_location()

    record = location.get("record", {})
    global_id = str(
        record.get(
            "globalIdLocal",
            record.get("global_id_local", IPMA_GLOBAL_ID_FALLBACK),
        )
    )

    print(f"Localidade IPMA selecionada: {global_id}")

    print("A recolher avisos IPMA...")
    warnings = fetch_ipma_warnings()

    print("A recolher previsao diaria IPMA...")
    ipma_daily = fetch_ipma_daily(global_id)

    print("A recolher previsoes IPMA de curto prazo...")
    ipma_hp = fetch_ipma_hp_days()

    print("A recolher previsao Open-Meteo/ECMWF...")
    open_meteo = fetch_open_meteo()

    print("A recolher contexto NOAA...")
    noaa = fetch_noaa_enso()

    sources = {
        "warnings": warnings,
        "ipma_daily": ipma_daily,
        "ipma_hp": ipma_hp,
        "open_meteo": open_meteo,
        "noaa": noaa,
    }

    print("A calcular risco...")
    risk = calculate_risk(
        ipma_daily,
        warnings,
        open_meteo,
    )

    print("A gerar relatorio...")
    report = generate_report(
        location,
        sources,
        risk,
        timestamp,
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    report_path = OUTPUT_DIR / "relatorio_latest.md"
    data_path = DATA_DIR / "dados_latest.json"

    report_path.write_text(
        report,
        encoding="utf-8",
    )

    data_path.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "location": location,
                "sources": sources,
                "risk": risk,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Relatorio criado: {report_path}")
    print(f"Dados criados: {data_path}")
    print(f"Risco operacional: {risk['overall']}")
    print(f"Score: {risk['score']}")


if __name__ == "__main__":
    main()
