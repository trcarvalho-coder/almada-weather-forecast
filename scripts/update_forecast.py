#!/usr/bin/env python3
"""Relatorio meteorologico para Funchalinho, Almada.

Fontes:
- IPMA: distrito, avisos, localidades e previsao diaria.
- IPMA stations: estacao de observacao mais proxima.
- Open-Meteo com ECMWF IFS: previsao auxiliar ate 7 dias.
- NOAA CPC ONI: contexto ENSO, sem uso como previsao local.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

LAT = 38.641
LON = -9.169
LOCATION_NAME = "Funchalinho, Almada"
TIMEOUT = 30

OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")
IPMA_GLOBAL_ID_FALLBACK = "1110600"

IPMA_DISTRICTS_URL = "https://api.ipma.pt/open-data/distrits-islands.json"
IPMA_STATIONS_URL = (
    "https://api.ipma.pt/open-data/observation/meteorology/stations/stations.json"
)
IPMA_WARNINGS_URL = (
    "https://api.ipma.pt/open-data/forecast/warnings/warnings_www.json"
)
IPMA_LOCATIONS_URL = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily"
)
IPMA_DAILY_TEMPLATE = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/"
    "{global_id}.json"
)
IPMA_HP_TEMPLATE = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/"
    "hp-daily-forecast-day{id_day}.json"
)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "funchalinho-weather-risk/1.0"})


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in (
            "data",
            "items",
            "locations",
            "localidades",
            "forecast",
            "forecastData",
            "stations",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def haversine_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    from math import asin, cos, radians, sin, sqrt

    radius = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    value = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1))
        * cos(radians(lat2))
        * sin(dlon / 2) ** 2
    )
    return 2 * radius * asin(sqrt(value))


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    text = str(value).strip()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def find_setubal_district() -> dict[str, Any]:
    try:
        payload = get_json(IPMA_DISTRICTS_URL)
        records = extract_records(payload)

        for item in records:
            text = json.dumps(item, ensure_ascii=False).lower()
            if "setúbal" in text or "setubal" in text:
                return {"status": "success", "record": item}

        return {"status": "not_found", "record": {}}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "record": {}}


def find_nearest_ipma_station() -> dict[str, Any]:
    try:
        payload = get_json(IPMA_STATIONS_URL)
        records = extract_records(payload)
        candidates: list[tuple[float, dict[str, Any]]] = []

        for item in records:
            latitude = first_value(item, ("latitude", "lat", "latitud"))
            longitude = first_value(item, ("longitude", "lon", "longitud"))

            if latitude is None or longitude is None:
                continue

            try:
                distance = haversine_km(
                    LAT,
                    LON,
                    float(latitude),
                    float(longitude),
                )
            except (TypeError, ValueError):
                continue

            candidates.append((distance, item))

        if not candidates:
            return {"status": "not_found", "record": {}, "distance_km": None}

        distance, station = min(candidates, key=lambda pair: pair[0])
        return {
            "status": "success",
            "record": station,
            "distance_km": distance,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "record": {},
            "distance_km": None,
        }


def find_ipma_location() -> dict[str, Any]:
    """Seleciona localidade de previsao diaria IPMA mais proxima, se possivel."""
    try:
        payload = get_json(IPMA_LOCATIONS_URL)
        records = extract_records(payload)
        candidates: list[tuple[float, dict[str, Any]]] = []

        for item in records:
            latitude = first_value(item, ("latitude", "lat"))
            longitude = first_value(item, ("longitude", "lon"))
            global_id = first_value(item, ("globalIdLocal", "global_id_local"))

            if latitude is None or longitude is None or global_id is None:
                continue

            try:
                distance = haversine_km(
                    LAT,
                    LON,
                    float(latitude),
                    float(longitude),
                )
            except (TypeError, ValueError):
                continue

            candidates.append((distance, item))

        if candidates:
            distance, record = min(candidates, key=lambda pair: pair[0])
            return {
                "status": "success",
                "record": record,
                "distance_km": distance,
            }

        return {
            "status": "fallback",
            "record": {
                "globalIdLocal": IPMA_GLOBAL_ID_FALLBACK,
                "localidade": "Almada - fallback",
            },
            "distance_km": None,
        }
    except Exception as exc:
        return {
            "status": "error_fallback",
            "error": str(exc),
            "record": {
                "globalIdLocal": IPMA_GLOBAL_ID_FALLBACK,
                "localidade": "Almada - fallback",
            },
            "distance_km": None,
        }


def warning_applies_to_setubal(item: dict[str, Any]) -> bool:
    text = json.dumps(item, ensure_ascii=False).lower()
    return any(term in text for term in ("setúbal", "setubal", "almada"))


def warning_intersects_next_three_days(item: dict[str, Any]) -> bool:
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=3)

    start = parse_datetime(
        first_value(
            item,
            ("startTime", "start", "inicio", "inicioAviso", "dtInicio", "validFrom"),
        )
    )
    end = parse_datetime(
        first_value(
            item,
            ("endTime", "end", "fim", "fimAviso", "dtFim", "validTo"),
        )
    )

    if start is None and end is None:
        return True
    if start is None:
        start = now
    if end is None:
        end = limit

    return start <= limit and end >= now


def fetch_ipma_warnings(district: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = get_json(IPMA_WARNINGS_URL)
        records = extract_records(payload)
        relevant: list[dict[str, Any]] = []

        district_record = district.get("record", {})
        district_id = first_value(
            district_record,
            ("idAreaAviso", "idDistrict", "id", "code"),
        )
        district_name = first_value(
            district_record,
            ("local", "name", "nome", "district", "districtName"),
        )

        for item in records:
            text = json.dumps(item, ensure_ascii=False).lower()
            item_area = str(
                first_value(
                    item,
                    (
                        "idAreaAviso",
                        "idDistrict",
                        "id",
                        "code",
                        "district",
                        "districtName",
                        "area",
                        "local",
                    ),
                )
                or ""
            ).lower()

            matches_id = (
                district_id is not None
                and str(district_id).lower() == item_area
            )
            matches_name = (
                district_name is not None
                and str(district_name).lower() in text
            )
            matches_text = "setúbal" in text or "setubal" in text

            if (matches_id or matches_name or matches_text) and warning_intersects_next_three_days(item):
                relevant.append(item)

        return {
            "source": "IPMA warnings",
            "status": "success",
            "district": district_record,
            "district_id": district_id,
            "district_name": district_name,
            "total_records": len(records),
            "relevant_count": len(relevant),
            "all": records,
            "relevant": relevant,
        }
    except Exception as exc:
        return {
            "source": "IPMA warnings",
            "status": "error",
            "error": str(exc),
            "total_records": 0,
            "relevant_count": 0,
            "all": [],
            "relevant": [],
        }


def fetch_ipma_daily(global_id: str) -> dict[str, Any]:
    url = IPMA_DAILY_TEMPLATE.format(global_id=global_id)
    try:
        return {
            "source": "IPMA daily",
            "status": "success",
            "url": url,
            "data": get_json(url),
        }
    except Exception as exc:
        return {
            "source": "IPMA daily",
            "status": "error",
            "url": url,
            "error": str(exc),
            "data": {},
        }


def fetch_ipma_hp_days() -> dict[str, Any]:
    result: dict[str, Any] = {}

    for day_id in range(1, 4):
        url = IPMA_HP_TEMPLATE.format(id_day=day_id)
        try:
            result[str(day_id)] = {
                "status": "success",
                "url": url,
                "data": get_json(url),
            }
        except Exception as exc:
            result[str(day_id)] = {
                "status": "error",
                "url": url,
                "error": str(exc),
                "data": {},
            }

    return {"source": "IPMA hp daily", "status": "success", "days": result}


def fetch_open_meteo() -> dict[str, Any]:
    """Recolhe previsao Open-Meteo usando ECMWF IFS e variaveis completas."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "models": "ecmwf_ifs025",
        "forecast_days": 7,
        "timezone": "Europe/Lisbon",
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "wind_speed_10m",
                "wind_gusts_10m",
                "pressure_msl",
                "weather_code",
            ]
        ),
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "rain_sum",
                "showers_sum",
                "wind_speed_10m_max",
                "wind_gusts_10m_max",
                "wind_direction_10m_dominant",
                "pressure_msl_mean",
                "weather_code",
            ]
        ),
        "hourly": ",".join(
            [
                "temperature_2m",
                "precipitation",
                "precipitation_probability",
                "rain",
                "showers",
                "wind_speed_10m",
                "wind_gusts_10m",
                "wind_direction_10m",
                "pressure_msl",
            ]
        ),
    }

    try:
        return {
            "source": "Open-Meteo ECMWF IFS",
            "status": "success",
            "data": get_json(OPEN_METEO_URL, params),
        }
    except Exception as exc:
        return {
            "source": "Open-Meteo ECMWF IFS",
            "status": "error",
            "error": str(exc),
            "data": {},
        }


def fetch_noaa_enso() -> dict[str, Any]:
    try:
        response = SESSION.get(NOAA_ONI_URL, timeout=TIMEOUT)
        response.raise_for_status()
        lines = [
            line.strip()
            for line in response.text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        return {
            "source": "NOAA CPC ONI",
            "status": "success",
            "url": NOAA_ONI_URL,
            "latest_lines": lines[-8:],
        }
    except Exception as exc:
        return {
            "source": "NOAA CPC ONI",
            "status": "error",
            "url": NOAA_ONI_URL,
            "error": str(exc),
            "latest_lines": [],
        }


def flatten_daily(payload: Any) -> list[dict[str, Any]]:
    records = extract_records(payload)
    if records:
        return records
    return []


def number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = item.get(key)
        if value in (None, "", "null"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def calculate_risk(
    ipma_daily: dict[str, Any],
    warnings: dict[str, Any],
    open_meteo: dict[str, Any],
) -> dict[str, Any]:
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
        actions.add("Seguir instrucoes da Protecao Civil")
    elif "laranja" in warning_text or '"orange"' in warning_text:
        score += 3
        actions.add("Preparar a habitacao e limitar deslocacoes")
    elif "amarelo" in warning_text or '"yellow"' in warning_text:
        score += 1
        actions.add("Monitorizar o aviso IPMA")

    for item in flatten_daily(ipma_daily.get("data", {}))[:5]:
        rain = number(
            item,
            "precipitaProb",
            "precipitationProbability",
            "precipitation",
            "precipitationSum",
        )
        wind = number(item, "predWindSpeed", "windSpeed", "wind_speed")
        gust = number(item, "windGust", "wind_gusts", "gust")
        tmax = number(item, "tMax", "temperatureMax", "temp_max")

        if rain is not None and rain >= 60:
            categories["rain"] = "HIGH"
            actions.add("Verificar caleiras, sumidouros e drenagem")
        elif rain is not None and rain >= 30:
            if categories["rain"] != "HIGH":
                categories["rain"] = "MEDIUM"
            actions.add("Monitorizar acumulacao de agua")

        if (gust is not None and gust >= 90) or (wind is not None and wind >= 70):
            if gust is not None and gust >= 100:
                categories["wind"] = "HIGH"
            elif categories["wind"] != "HIGH":
                categories["wind"] = "MEDIUM"
            actions.add("Fixar objetos exteriores e verificar a cobertura")

        if tmax is not None and tmax >= 35:
            categories["temperature"] = "HIGH"
            actions.add("Limitar exposicao ao calor e proteger vulneraveis")
        elif tmax is not None and tmax >= 30:
            if categories["temperature"] != "HIGH":
                categories["temperature"] = "MEDIUM"
            actions.add("Manter hidratacao e proteger vulneraveis")

    if categories["rain"] != "LOW" or categories["wind"] != "LOW":
        categories["coastal"] = "MEDIUM"
        actions.add("Evitar arribas e acessos costeiros durante temporal")

    om_daily = open_meteo.get("data", {}).get("daily", {})
    om_rain = om_daily.get("precipitation_sum", [])[:3]
    om_wind = om_daily.get("wind_gusts_10m_max", [])[:3]

    if any(isinstance(value, (int, float)) and value >= 40 for value in om_rain):
        categories["rain"] = "HIGH" if categories["rain"] == "LOW" else categories["rain"]
        actions.add("Confirmar chuva nas atualizacoes IPMA")

    if any(isinstance(value, (int, float)) and value >= 90 for value in om_wind):
        if categories["wind"] == "LOW":
            categories["wind"] = "MEDIUM"
        actions.add("Confirmar vento nas atualizacoes IPMA")

    if categories["temperature"] == "HIGH":
        score += 3
    elif categories["temperature"] == "MEDIUM":
        score += 1

    if categories["rain"] == "HIGH":
        score += 3
    elif categories["rain"] == "MEDIUM":
        score += 1

    if categories["wind"] == "HIGH":
        score += 3
    elif categories["wind"] == "MEDIUM":
        score += 1

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
    location_record = location.get("record", {})
    global_id = first_value(
        location_record,
        ("globalIdLocal", "global_id_local"),
    ) or IPMA_GLOBAL_ID_FALLBACK

    station = sources.get("nearest_station", {})
    station_record = station.get("record", {})
    station_name = first_value(
        station_record,
        ("name", "nome", "stationName", "local", "idEstacao"),
    ) or "N/D"
    station_id = first_value(
        station_record,
        ("idEstacao", "stationId", "id", "codigo"),
    ) or "N/D"
    station_distance = station.get("distance_km")
    station_distance_text = (
        f"{station_distance:.1f} km"
        if isinstance(station_distance, (int, float))
        else "N/D"
    )

    lines = [
        f"# Relatorio meteorologico - {LOCATION_NAME}",
        "",
        f"**Atualizado:** {timestamp}",
        f"**Coordenadas:** {LAT}, {LON}",
        f"**GlobalIdLocal IPMA:** {global_id}",
        "**Localidade IPMA:** Almada ou localidade automatica mais proxima",
        "",
        "> Este relatorio apoia a preparacao local. Nao substitui avisos oficiais.",
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
    lines.extend(
        f"- [ ] {action}" for action in risk["actions"]
    )
    if not risk["actions"]:
        lines.append("- Sem acoes adicionais identificadas.")

    lines.extend(
        [
            "",
            "## Estacao IPMA mais proxima",
            "",
            f"- **Nome:** {station_name}",
            f"- **Identificador:** {station_id}",
            f"- **Distancia aproximada:** {station_distance_text}",
        ]
    )

    ipma_records = flatten_daily(
        sources.get("ipma_daily", {}).get("data", {})
    )
    lines.extend(
        [
            "",
            "## Previsao diaria IPMA ate 5 dias",
            "",
            "| Data | Temp. maxima | Precipitacao/probabilidade | Vento |",
            "|---|---:|---:|---:|",
        ]
    )

    for item in ipma_records[:5]:
        date = first_value(item, ("forecastDate", "date", "time")) or "N/D"
        temp = first_value(item, ("tMax", "temperatureMax", "temp_max")) or "N/D"
        rain = first_value(
            item,
            ("precipitaProb", "precipitationProbability", "precipitation"),
        ) or "N/D"
        wind = first_value(item, ("predWindSpeed", "windSpeed", "wind_speed")) or "N/D"
        lines.append(f"| {date} | {temp} | {rain} | {wind} |")

    if not ipma_records:
        lines.append("| Dados IPMA nao disponiveis | N/D | N/D | N/D |")

    om = sources.get("open_meteo", {})
    om_daily = om.get("data", {}).get("daily", {})
    lines.extend(
        [
            "",
            "## Previsao Open-Meteo / ECMWF IFS ate 7 dias",
            "",
            "| Data | Temp. maxima | Temp. minima | Chuva | Prob. chuva | Rajadas |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )

    om_dates = om_daily.get("time", [])
    om_tmax = om_daily.get("temperature_2m_max", [])
    om_tmin = om_daily.get("temperature_2m_min", [])
    om_rain = om_daily.get("precipitation_sum", [])
    om_probability = om_daily.get("precipitation_probability_max", [])
    om_gusts = om_daily.get("wind_gusts_10m_max", [])

    for index in range(min(7, len(om_dates))):
        def get_at(values: list[Any], default: Any = "N/D") -> Any:
            return values[index] if index < len(values) else default

        lines.append(
            f"| {get_at(om_dates)} | {get_at(om_tmax)} | "
            f"{get_at(om_tmin)} | {get_at(om_rain)} | "
            f"{get_at(om_probability)} | {get_at(om_gusts)} |"
        )

    if not om_dates:
        lines.append("| Dados Open-Meteo indisponiveis | N/D | N/D | N/D | N/D | N/D |")

    warning_source = sources.get("warnings", {})
    warnings = warning_source.get("relevant", [])
    lines.extend(["", "## Avisos IPMA - distrito de Setubal", ""])

    if warnings:
        lines.append(f"**Avisos encontrados:** {len(warnings)}")
        for warning in warnings:
            lines.append(
                f"- `{json.dumps(warning, ensure_ascii=False)}`"
            )
    else:
        lines.append(
            "Nao foram identificados avisos IPMA aplicaveis a Setubal/Almada."
        )
        lines.append(
            f"Registos recebidos: {warning_source.get('total_records', 'N/D')}"
        )

    lines.extend(
        [
            "",
            "## Fontes",
            "",
            f"- IPMA distritos: {IPMA_DISTRICTS_URL}",
            f"- IPMA estacoes: {IPMA_STATIONS_URL}",
            f"- IPMA avisos: {IPMA_WARNINGS_URL}",
            f"- IPMA previsao diaria: {IPMA_DAILY_TEMPLATE.format(global_id=global_id)}",
            "- Open-Meteo com ECMWF IFS: previsao numerica auxiliar.",
            "- NOAA CPC ONI: contexto ENSO, nao previsao local.",
            "- ECMWF/Copernicus: referencia para previsao sazonal.",
            "",
            "## Limites",
            "",
            "O score e um indicador de severidade operacional prevista. "
            "Nao representa a probabilidade estatistica de um desastre local.",
        ]
    )

    return "\n".join(lines) + "\n"


def main() -> None:
    timestamp = datetime.now(
        ZoneInfo("Europe/Lisbon")
    ).strftime("%Y-%m-%d %H:%M %Z")

    print("A obter distrito de Setubal...")
    district = find_setubal_district()

    print("A procurar estacao IPMA mais proxima...")
    station = find_nearest_ipma_station()

    print("A procurar localidade IPMA de previsao...")
    location = find_ipma_location()
    location_record = location.get("record", {})
    global_id = str(
        first_value(
            location_record,
            ("globalIdLocal", "global_id_local"),
        ) or IPMA_GLOBAL_ID_FALLBACK
    )

    print("A recolher avisos IPMA...")
    warnings = fetch_ipma_warnings(district)
    print(
        f"Avisos recebidos: {warnings.get('total_records', 0)}; "
        f"relevantes: {warnings.get('relevant_count', 0)}"
    )

    print("A recolher previsao IPMA...")
    ipma_daily = fetch_ipma_daily(global_id)
    ipma_hp = fetch_ipma_hp_days()

    print("A recolher Open-Meteo / ECMWF IFS...")
    open_meteo = fetch_open_meteo()

    print("A recolher NOAA ONI...")
    noaa = fetch_noaa_enso()

    sources = {
        "district": district,
        "nearest_station": station,
        "warnings": warnings,
        "ipma_daily": ipma_daily,
        "ipma_hp": ipma_hp,
        "open_meteo": open_meteo,
        "noaa": noaa,
    }

    risk = calculate_risk(ipma_daily, warnings, open_meteo)
    report = generate_report(location, sources, risk, timestamp)

    OUTPUT_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    report_path = OUTPUT_DIR / "relatorio_latest.md"
    data_path = DATA_DIR / "dados_latest.json"

    report_path.write_text(report, encoding="utf-8")
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
    print(f"Risco: {risk['overall']} | Score: {risk['score']}")


if __name__ == "__main__":
    main()
