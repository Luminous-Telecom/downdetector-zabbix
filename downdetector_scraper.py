#!/usr/bin/env python3
"""
Downdetector BR → Zabbix Agent

Modelo:
  - Cada host no Zabbix = um serviço (Host name = slug, ex.: whatsapp)
  - Agent só lê cache local (ms) → downdetector.status[{HOST.HOST}]
  - Timer systemd atualiza o cache via FlareSolverr (API lista os hosts)

Exemplos:
  python3 downdetector_scraper.py --from-cache --service whatsapp
  python3 downdetector_scraper.py --refresh-all --workers 2
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

DEFAULT_FLARESOLVERR_URL = os.environ.get(
    "FLARESOLVERR_URL", "http://127.0.0.1:8191/v1"
)
DEFAULT_CACHE_DIR = os.environ.get(
    "DOWNDETECTOR_CACHE_DIR", "/var/cache/downdetector-zabbix"
)
BASE_URL = "https://downdetector.com.br/"
SERVICE_URL = BASE_URL + "fora-do-ar/{slug}/"
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,80}$")

STATUS_CODE = {"success": 1, "warning": 2, "danger": 3, "unknown": 0}
STATUS_FROM_TEXT = [
    ("sem problemas", "success"),
    ("possíveis problemas", "warning"),
    ("problemas", "danger"),
]
STATS_STATUS_RE = re.compile(r'\\"status\\":\\"(success|warning|danger)\\"')
DATA_POINTS_KEY = '\\"dataPoints\\":'


@dataclass
class ServiceStatus:
    slug: str
    name: str
    logo: str
    status: str
    status_code: int
    reports: int | None
    reports_baseline: int | None
    reports_at: str | None
    url: str


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug inválido: {slug!r}")
    return slug


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def status_from_text(text: str) -> str:
    normalized = normalize(text)
    for phrase, status in STATUS_FROM_TEXT:
        if phrase in normalized:
            return status
    return "unknown"


def is_cloudflare_challenge(html: str) -> bool:
    if "dataPoints" in html or "fora do ar?" in html.lower():
        return False
    return any(
        m in html
        for m in ("Just a moment", "Um momento", "cf-chl-opt", "Enable JavaScript")
    )


# --- FlareSolverr ---


def flaresolverr_get(
    url: str,
    api: str,
    timeout: int = 60,
    session_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max(timeout, 60) * 1000,
    }
    if session_id:
        payload["session"] = session_id
    resp = requests.post(api, json=payload, timeout=max(timeout, 120))
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(data.get("message", "erro FlareSolverr"))
    sol = data.get("solution") or {}
    if sol.get("status") != 200:
        raise RuntimeError(f"HTTP {sol.get('status')}")
    html = sol.get("response") or ""
    if not html or is_cloudflare_challenge(html):
        raise RuntimeError("desafio Cloudflare / HTML vazio")
    return html


class FlareSolverrPool:
    """Sessões Chrome quentes para refresh paralelo controlado."""

    def __init__(self, api: str, size: int) -> None:
        self.api = api
        self.size = max(1, size)
        self._sessions: list[str] = []
        self._free: queue.Queue[str] = queue.Queue()

    def __enter__(self) -> FlareSolverrPool:
        print(f"FlareSolverr: {self.size} sessão(ões)...", file=sys.stderr, flush=True)
        for _ in range(self.size):
            r = requests.post(self.api, json={"cmd": "sessions.create"}, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok" or not data.get("session"):
                raise RuntimeError(data.get("message", "falha sessions.create"))
            self._sessions.append(data["session"])

        def warm(sid: str) -> None:
            flaresolverr_get(BASE_URL, self.api, 120, session_id=sid)

        with ThreadPoolExecutor(max_workers=self.size) as ex:
            list(ex.map(warm, self._sessions))
        for sid in self._sessions:
            self._free.put(sid)
        print("FlareSolverr: pronto.", file=sys.stderr, flush=True)
        return self

    def __exit__(self, *exc: object) -> None:
        for sid in self._sessions:
            try:
                requests.post(
                    self.api,
                    json={"cmd": "sessions.destroy", "session": sid},
                    timeout=30,
                )
            except Exception:
                pass
        self._sessions.clear()

    def get(self, url: str, timeout: int = 60) -> str:
        sid = self._free.get()
        try:
            return flaresolverr_get(url, self.api, timeout, session_id=sid)
        finally:
            self._free.put(sid)


# --- Parse ---


def extract_chart_data(html: str) -> tuple[str | None, list[dict[str, Any]]]:
    status_m = STATS_STATUS_RE.search(html)
    status = status_m.group(1) if status_m else None
    key_idx = html.find(DATA_POINTS_KEY)
    if key_idx == -1:
        return status, []
    start = key_idx + len(DATA_POINTS_KEY)
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start=start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        points = json.loads(html[start:end].replace('\\"', '"'))
    except json.JSONDecodeError:
        return status, []
    return status, points


def parse_service_page(html: str, slug: str) -> ServiceStatus:
    soup = BeautifulSoup(html, "html.parser")
    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")

    name = slug
    if og_title and og_title.get("content"):
        name = og_title["content"].split(" fora do ar?")[0].strip() or slug
    logo = og_image.get("content", "") if og_image else ""

    status, points = extract_chart_data(html)
    reports = reports_baseline = reports_at = None
    if points:
        last = points[-1]
        reports = last.get("reportsValue")
        reports_baseline = last.get("baselineValue")
        reports_at = last.get("timestampUtc")

    status = status or "unknown"
    if status == "unknown":
        for chart in soup.select('[role="img"]'):
            aria = chart.get("aria-label", "")
            m = re.search(r"status:\s*(.+)$", aria, re.I)
            if m:
                status = status_from_text(m.group(1))
                break
    if status == "unknown":
        h1 = soup.select_one("h1")
        if h1:
            status = status_from_text(h1.get_text(strip=True))

    return ServiceStatus(
        slug=slug,
        name=name,
        logo=logo,
        status=status,
        status_code=STATUS_CODE.get(status, 0),
        reports=reports,
        reports_baseline=reports_baseline,
        reports_at=reports_at,
        url=SERVICE_URL.format(slug=slug),
    )


# --- Cache ---


def cache_path(cache_dir: str, slug: str) -> str:
    return os.path.join(cache_dir, f"{slug}.json")


def write_cache(cache_dir: str, service: ServiceStatus) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = cache_path(cache_dir, service.slug)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(service), fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_cache(cache_dir: str, slug: str) -> dict[str, Any]:
    slug = validate_slug(slug)
    path = cache_path(cache_dir, slug)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"cache ausente: {path} — espere o timer ou rode --refresh-all"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- Zabbix API ---


def zabbix_rpc(
    url: str,
    method: str,
    params: dict[str, Any],
    token: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    if method != "user.login" and token:
        payload["auth"] = token.strip()
    r = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json-rpc"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        err = data["error"]
        raise RuntimeError(f"API {method}: {err.get('message')} — {err.get('data')}")
    return data.get("result")


def list_hosts_from_zabbix(
    url: str,
    token: str,
    template_name: str = "downdetector",
) -> list[str]:
    token = token.strip()
    templates = zabbix_rpc(
        url,
        "template.get",
        {"output": ["templateid"], "filter": {"host": [template_name]}},
        token,
    )
    if not templates:
        templates = zabbix_rpc(
            url,
            "template.get",
            {"output": ["templateid"], "filter": {"name": [template_name]}},
            token,
        )
    if not templates:
        raise RuntimeError(f"template {template_name!r} não encontrado")

    hosts = zabbix_rpc(
        url,
        "host.get",
        {
            "output": ["host"],
            "templateids": [templates[0]["templateid"]],
            "filter": {"status": 0},
        },
        token,
    )
    slugs: list[str] = []
    for h in hosts or []:
        slug = (h.get("host") or "").strip()
        if SLUG_RE.match(slug) and slug not in slugs:
            slugs.append(slug)
    return slugs


def list_cached_slugs(cache_dir: str) -> list[str]:
    if not os.path.isdir(cache_dir):
        return []
    out: list[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if name.endswith(".json") and name != "lld.json":
            slug = name[:-5]
            if SLUG_RE.match(slug):
                out.append(slug)
    return out


# --- Refresh ---


def fetch_one(slug: str, pool: FlareSolverrPool) -> ServiceStatus:
    html = pool.get(SERVICE_URL.format(slug=validate_slug(slug)))
    return parse_service_page(html, slug)


def refresh_all(
    *,
    cache_dir: str,
    flaresolverr_url: str,
    workers: int,
    zabbix_url: str | None,
    zabbix_token: str | None,
    template_name: str,
) -> int:
    slugs: list[str] = []
    if zabbix_url and zabbix_token and zabbix_token not in (
        "",
        "COLE_O_TOKEN_AQUI",
        "cole_aqui_o_token_gerado_no_zabbix",
    ):
        slugs = list_hosts_from_zabbix(zabbix_url, zabbix_token, template_name)
        print(f"API Zabbix: {len(slugs)} host(s)", file=sys.stderr, flush=True)
    for s in list_cached_slugs(cache_dir):
        if s not in slugs:
            slugs.append(s)

    if not slugs:
        print(
            "Nenhum host. Cadastre hosts no Zabbix com template "
            f"{template_name!r} e configure /etc/zabbix/downdetector-api.env",
            file=sys.stderr,
        )
        return 1

    workers = max(1, workers)
    total = len(slugs)
    ok = fail = 0
    done = 0
    lock = threading.Lock()

    with FlareSolverrPool(flaresolverr_url, workers) as pool:

        def one(slug: str) -> bool:
            nonlocal done, ok, fail
            try:
                svc = fetch_one(slug, pool)
                write_cache(cache_dir, svc)
                msg = f"{svc.status} reports={svc.reports}"
                success = True
            except Exception as exc:
                msg = f"ERRO {exc}"
                success = False
            with lock:
                done += 1
                if success:
                    ok += 1
                else:
                    fail += 1
                print(f"[{done}/{total}] {slug}: {msg}", file=sys.stderr, flush=True)
            return success

        if workers == 1:
            for s in slugs:
                one(s)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(one, slugs))

    print(
        json.dumps(
            {"updated": ok, "failed": fail, "total": total, "workers": workers},
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


# --- CLI ---


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Downdetector BR para Zabbix Agent")
    p.add_argument("--service", help="Slug (Host name no Zabbix)")
    p.add_argument(
        "--from-cache",
        action="store_true",
        help="Só lê disco (UserParameter do agent)",
    )
    p.add_argument(
        "--refresh-all",
        action="store_true",
        help="Atualiza cache via FlareSolverr (timer systemd)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("DOWNDETECTOR_WORKERS", "2")),
        help="Sessões FlareSolverr em paralelo (padrão: 2)",
    )
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    p.add_argument("--flaresolverr-url", default=DEFAULT_FLARESOLVERR_URL)
    p.add_argument("--zabbix-url", default=os.environ.get("ZABBIX_URL"))
    p.add_argument("--zabbix-token", default=os.environ.get("ZABBIX_TOKEN"))
    p.add_argument(
        "--zabbix-template",
        default=os.environ.get("ZABBIX_TEMPLATE", "downdetector"),
    )
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.refresh_all:
        return refresh_all(
            cache_dir=args.cache_dir,
            flaresolverr_url=args.flaresolverr_url,
            workers=args.workers,
            zabbix_url=args.zabbix_url,
            zabbix_token=args.zabbix_token,
            template_name=args.zabbix_template,
        )

    if not args.service:
        print("use: --from-cache --service SLUG  |  --refresh-all", file=sys.stderr)
        return 1

    try:
        if args.from_cache:
            payload = read_cache(args.cache_dir, args.service)
        else:
            # coleta avulsa (debug)
            with FlareSolverrPool(args.flaresolverr_url, 1) as pool:
                svc = fetch_one(args.service, pool)
            write_cache(args.cache_dir, svc)
            payload = asdict(svc)
    except Exception as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
