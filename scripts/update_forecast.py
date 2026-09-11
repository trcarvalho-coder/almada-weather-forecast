#!/usr/bin/env python3
"""Previsao e risco local para Almada.

Fontes:
- IPMA: avisos oficiais e previsao diaria local.
- Open-Meteo com ECMWF IFS: previsao numerica auxiliar.
- NOAA CPC: contexto ENSO/ONI.

O script nao calcula a probabilidade estatistica de um desastre.
Classifica a severidade operacional prevista e mostra avisos oficiais.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

LAT = 38.641
LON = -9.169
LOCATION_NAME = "Funchalinho, Almada"
TIMEOUT = 30

OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")

# Almada e usado apenas como fallback se a pesquisa automatica falhar.
IPMA_GLOBAL_ID_FALLBACK = "1110600"

IPMA_WARNINGS_URL = (
    "https://api.ipma.pt/open-data/forecast/warnings/warnings_www.json"
)
IPMA_DAILY_TEMPLATE = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/"
    "{global_id}.json"
)
IPMA_HP_TEMPLATE = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/"
    "hp-daily-forecast-day{id_day}.json"
)
IPMA_LOCATIONS_URL = (
    "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily"
)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": "funchalinho-weather-risk/1.0"}
)


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    response = SESSION.get(
        url,
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Extrai listas de registos de diferentes estruturas JSON."""
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
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


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


def first_value(
    item: dict[str, Any],
    keys: tuple[str, ...],
) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    text = str(value).strip()

    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
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
            return datetime.strptime(
                text,
                fmt,
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def find_ipma_location() -> dict[str, Any]:
    """Seleciona a localidade IPMA mais proxima das coordenadas indicadas."""
    try:
        payload = get_json(IPMA_LOCATIONS_URL)
        records = extract_records(payload)
        candidates: list[tuple[float, dict[str, Any]]] = []

        for item in records:
            latitude = first_value(
                item,
                ("latitude", "lat"),
            )
            longitude = first_value(
                item,
                ("longitude", "lon"),
            )
            global_id = first_value(
                item,
                ("globalIdLocal", "global_id_local"),
            )

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
            distance, record = min(
                candidates,
                key=lambda pair: pair[0],
            )
            return {
                "status": "success",
                "record": record,
                "distance_km": distance,
            }

        return {
            "status": "fallback",
            "record": {
                "globalIdLocal": IPMA_GLOBAL_ID_FALLBACK,
                "localidade": "Almada (fallback)",
            },
            "distance_km": None,
        }

    except Exception as exc:
        return {
            "status": "error_fallback",
            "error": str(exc),
            "record": {
                "globalIdLocal": IPMA_GLOBAL_ID_FALLBACK,
                "localidade": "Almada (fallback)",
            },
            "distance_km": None,
        }


def warning_area_text(item: dict[str, Any]) -> str:
    value = first_value(
        item,
        (
            "idAreaAviso",
            "idArea",
            "area",
            "areaAviso",
            "district",
            "districtName",
            "local",
            "localidade",
        ),
    )

    return str(value or "").strip().lower()


def warning_applies_to_setubal(item: dict[str, Any]) -> bool:
    """Identifica avisos de Setubal/Almada no payload do IPMA."""
    area = warning_area_text(item)
    text = json.dumps(
        item,
        ensure_ascii=False,
    ).lower()

    known_area_values = {
        "set",
        "setubal",
        "setúbal",
        "17",
        "1700",
        "17-setubal",
    }

    if area in known_area_values:
        return True

    return any(
        term in text
        for term in ("setúbal", "setubal", "almada")
    )


def warning_intersects_next_three_days(
    item: dict[str, Any],
) -> bool:
    """Verifica se o aviso e valido agora ou nos proximos 3 dias."""
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=3)

    start_value = first_value(
        item,
        (
            "startTime",
            "start",
            "inicio",
            "inicioAviso",
            "dtInicio",
            "dateStart",
            "validFrom",
        ),
    )
    end_value = first_value(
        item,
        (
            "endTime",
            "end",
            "fim",
            "fimAviso",
            "dtFim",
            "dateEnd",
            "validTo",
        ),
    )

    start = parse_datetime(start_value)
    end = parse_datetime(end_value)

    # Se o esquema nao apresentar datas, o endpoint e tratado como lista
    # de avisos atuais e o registo nao e descartado.
    if start is None and end is None:
        return True

    if start is None:
        start = now
    if end is None:
        end = limit

    return start <= limit and end >= now


def fetch_ipma_warnings() -> dict[str, Any]:
    """Recolhe e filtra avisos IPMA para Setubal/Almada."""
    try:
        payload = get_json(IPMA_WARNINGS_URL)
        records = extract_records(payload)
        relevant: list[dict[str, Any]] = []

        for item in records:
            if not warning_applies_to_setubal(item):
                continue
            if not warning_intersects_next_three_days(item):
                continue
            relevant.append(item)

        return {
            "source": "IPMA warnings",
            "status": "success",
            "retrieved_at": datetime.now(
                timezone.utc
            ).isoformat(),
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
    """Recolhe a previsao IPMA local ate cinco dias."""
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
    """Recolhe os endpoints IPMA hp-daily dos dias 1 a 3."""
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

    return {
        "source": "IPMA high priority daily",
        "status": "success",
        "days": result,
    }


def fetch_open_meteo() -> dict[str, Any]:
    """Recolhe previsao auxiliar do ECMWF IFS via Open-Meteo."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": ",".join(
            [
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "wind_gusts_10m_max",
                "temperature_2m_max",
                "temperature_2m_min",
                "pressure_msl_mean",
                "weather_code",
            ]
        ),
        "hourly": ",".join(
            [
                "precipitation",
                "precipitation_probability",
                "wind_speed_10m",
                "wind_gusts_10m",
                "pressure_msl",
            ]
        ),
        "forecast_days": 7,
        "timezone": "Europe/Lisbon",
        "models": "ecmwf_ifs025",
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
    """Recolhe contexto ONI da NOAA CPC; nao e previsao local."""
    try:
        response = SESSION.get(
            NOAA_ONI_URL,
            timeout=TIMEOUT,
        )
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

    if isinstance(payload, dict):
        for key in ("forecast", "forecastData", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    item for item in value
                    if isinstance(item, dict)
                ]

    return []


def number(
    item: dict[str, Any],
    *keys: str,
) -> float | None:
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
    """Calcula severidade operacional dos proximos tres dias."""
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
            "Seguir imediatamente as instrucoes da Protecao Civil"
        )
    elif "laranja" in warning_text or '"orange"' in warning_text:
        score += 3
        actions.add(
            "Preparar a habitacao e limitar deslocacoes"
        )
    elif "amarelo" in warning_text or '"yellow"' in warning_text:
        score += 1
        actions.add(
            "Monitorizar o aviso e a atualizacao seguinte do IPMA"
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
                "Monitorizar acumulacao de agua"
            )

        if (
            (gust is not None and gust >= 90)
            or (wind is not None and wind >= 70)
        ):
            if gust is not None and gust >= 100:
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
                "Manter hidratacao e proteger pessoas vulneraveis"
            )

    if (
        categories["rain"] != "LOW"
        or categories["wind"] != "LOW"
    ):
        categories["coastal"] = "MEDIUM"
        actions.add(
            "Evitar arribas e acessos costeiros durante temporal"
        )

    if open_meteo.get("status") == "success":
        daily = open_meteo.get("data", {}).get("daily", {})

        for rain_value in daily.get("precipitation_sum", [])[:3]:
            if (
                isinstance(rain_value, (int, float))
                and rain_value >= 40
            ):
                score += 1
                actions.add(
                    "Confirmar a evolucao da chuva nas atualizacoes IPMA"
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
    global_id = first_value(
        record,
        ("globalIdLocal", "global_id_local"),
    ) or IPMA_GLOBAL_ID_FALLBACK

    distance = location.get("distance_km")
    distance_text = (
        f"{distance:.1f} km"
        if isinstance(distance, (int, float))
        else "nao determinada"
    )

    lines = [
        f"# Relatorio meteorologico - {LOCATION_NAME}",
        "",
        f"**Atualizado:** {timestamp}",
        f"**Coordenadas:** {LAT}, {LON}",
        f"**GlobalIdLocal IPMA:** {global_id}",
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

    daily_records = flatten_daily(
        sources.get("ipma_daily", {}).get("data", {})
    )

    lines.extend(
        [
            "",
            "## Previsao diaria IPMA ate 5 dias",
            "",
            "| Data | Temperatura maxima | Precipitacao/probabilidade | Vento |",
            "|---|---:|---:|---:|",
        ]
    )

    for item in daily_records[:5]:
        date_value = first_value(
            item,
            ("forecastDate", "date", "time"),
        ) or "N/D"
        temperature = first_value(
            item,
            ("tMax", "temperatureMax", "temp_max"),
        )
        precipitation = first_value(
            item,
            (
                "precipitaProb",
                "precipitationProbability",
                "precipitation",
            ),
        )
        wind = first_value(
            item,
            ("predWindSpeed", "windSpeed", "wind_speed"),
        )

        lines.append(
            f"| {date_value} | {temperature or 'N/D'} | "
            f"{precipitation or 'N/D'} | {wind or 'N/D'} |"
        )

    if not daily_records:
        lines.append(
            "| Dados IPMA nao disponiveis ou estrutura nao reconhecida | N/D | N/D | N/D |"
        )

    lines.extend(
        [
            "",
            "## Avisos IPMA para os proximos 3 dias",
            "",
        ]
    )

    warning_source = sources.get("warnings", {})
    warnings = warning_source.get("relevant", [])

    if warnings:
        lines.append(f"**Avisos encontrados:** {len(warnings)}")
        lines.append("")

        for warning in warnings:
            area = first_value(
                warning,
                (
                    "idAreaAviso",
                    "area",
                    "district",
                    "local",
                ),
            ) or "N/D"
            phenomenon = first_value(
                warning,
                (
                    "awarenessTypeName",
                    "phenomenon",
                    "type",
                    "description",
                ),
            ) or "N/D"
            level = first_value(
                warning,
                (
                    "awarenessLevelID",
                    "level",
                    "color",
                    "severity",
                ),
            ) or "N/D"
            start = first_value(
                warning,
                ("startTime", "start", "dtInicio"),
            ) or "N/D"
            end = first_value(
                warning,
                ("endTime", "end", "dtFim"),
            ) or "N/D"

            lines.extend(
                [
                    f"- **Area:** {area}",
                    f"  **Fenomeno:** {phenomenon}",
                    f"  **Nivel:** {level}",
                    f"  **Inicio:** {start}",
                    f"  **Fim:** {end}",
                ]
            )
    else:
        lines.append(
            "Nao foram identificados avisos IPMA aplicaveis a Setubal/Almada."
        )
        lines.append(
            f"Registos recebidos pelo endpoint: "
            f"{warning_source.get('total_records', 'N/D')}"
        )

    if warning_source.get("status") != "success":
        lines.append("")
        lines.append(
            f"Erro na consulta aos avisos IPMA: "
            f"{warning_source.get('error', 'erro desconhecido')}"
        )

    lines.extend(
        [
            "",
            "## Outras fontes",
            "",
            f"- IPMA avisos: {IPMA_WARNINGS_URL}",
            f"- IPMA previsao local: {IPMA_DAILY_TEMPLATE.format(global_id=global_id)}",
            f"- IPMA curto prazo dias 1-3: {IPMA_HP_TEMPLATE.format(id_day='{idDay}')}",
            "- Open-Meteo com ECMWF IFS: previsao numerica auxiliar ate 7 dias.",
            "- NOAA CPC ONI: contexto ENSO, nao previsao local.",
            "- ECMWF/Copernicus: referencia adequada para previsao sazonal.",
            "",
            "## Limites",
            "",
            "O score e um indicador de severidade operacional prevista. "
            "Nao representa a probabilidade estatistica de um desastre local.",
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
        first_value(
            record,
            ("globalIdLocal", "global_id_local"),
        ) or IPMA_GLOBAL_ID_FALLBACK
    )

    print(f"GlobalIdLocal IPMA: {global_id}")

    print("A recolher avisos IPMA...")
    warnings = fetch_ipma_warnings()
    print(
        f"Avisos recebidos: {warnings.get('total_records', 0)}; "
        f"relevantes: {warnings.get('relevant_count', 0)}"
    )

    print("A recolher previsao diaria IPMA...")
    ipma_daily = fetch_ipma_daily(global_id)

    print("A recolher previsao IPMA dos dias 1-3...")
    ipma_hp = fetch_ipma_hp_days()

    print("A recolher previsao auxiliar ECMWF IFS...")
    open_meteo = fetch_open_meteo()

    print("A recolher contexto NOAA ONI...")
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
    print(f"Risco: {risk['overall']} | Score: {risk['score']}")


if __name__ == "__main__":
    main()
