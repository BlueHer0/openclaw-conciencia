#!/usr/bin/env python3
"""
conciencia.py — curador nocturno de SOUL/TOOLS/MEMORY con flujo de 6 prompts.

CONFIGURACIÓN
  Primary  : Mistral Large 3 vía NVIDIA NIM (no toca VRAM de Spark)
  Fallback : Qwen3.6:35b-a3b en Ollama local (LAN, /api/generate)
  Output   : ~/.openclaw/workspace/_conciencia/propuestas/YYYY-MM-DD/
  Backups  : últimos 5 en _conciencia/backups/YYYY-MM-DD/ (rotación)
  Log      : ~/.openclaw/logs/conciencia.log

FLUJO
  Prompt 0 — Pre-flight (archivos + gateway)
  Prompt 1 — DIAGNOSTICO global
  Prompt 2 — SOUL.md nuevo
  Prompt 3 — MEMORY.md nuevo
  Prompt 4 — TOOLS.md nuevo
  Prompt 5 — VALIDACION cruzada
  Prompt 6 — Apply (solo --apply y solo si VALIDACION = APROBADO)

USO
  conciencia.py --dry-run                 → prompts 0-5, propone, NO aplica
  conciencia.py --apply                   → prompts 0-6, aplica si validación aprueba
  conciencia.py --rollback YYYY-MM-DD     → restaura backup de esa fecha
  conciencia.py --status                  → muestra última propuesta y backups
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ─── paths y constantes ───────────────────────────────────────────────────────
HOME       = Path.home()
WORKSPACE  = HOME / ".openclaw" / "workspace"
CONCIENCIA = WORKSPACE / "_conciencia"
PROPUESTAS = CONCIENCIA / "propuestas"
BACKUPS    = CONCIENCIA / "backups"
CONSTIT    = CONCIENCIA / "CONSTITUTION.md"
SOUL_F     = WORKSPACE / "SOUL.md"
TOOLS_F    = WORKSPACE / "TOOLS.md"
MEMORY_F   = WORKSPACE / "MEMORY.md"
SECRETS    = HOME / ".openclaw" / "secrets.env"
USER_LOGS  = HOME / ".openclaw" / "logs"
RUNTIME_L  = Path("/tmp/openclaw")
CONC_LOG   = USER_LOGS / "conciencia.log"
OPENCLAW   = "/usr/local/bin/openclaw"
GATEWAY_HEALTH = "http://127.0.0.1:18789/health"

# LLMs
NVIDIA_BASE        = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL       = "mistralai/mistral-large-3-675b-instruct-2512"
NVIDIA_TIMEOUT_SEC = 600
OLLAMA_BASE        = "http://OLLAMA_HOST:11434"   # ← SANITIZAR: IP de tu host Ollama
OLLAMA_GENERATE    = OLLAMA_BASE + "/api/generate"
OLLAMA_MODEL       = "qwen3.6:35b-a3b"
OLLAMA_TIMEOUT_SEC = 1800
NUM_CTX_OLLAMA     = 200000
TEMPERATURE        = 0.3

# Política
MAX_BACKUPS            = 5
MAX_LOG_HITS           = 500
MAX_RETRIES_PER_PROMPT = 3
MAX_TOKENS_DIAG        = 8000
MAX_TOKENS_FILE        = 16000
MAX_TOKENS_VALID       = 4000


# ─── helpers ──────────────────────────────────────────────────────────────────
def secret(name: str, default: str | None = None) -> str | None:
    try:
        m = re.search(rf"^{re.escape(name)}=(.+)$", SECRETS.read_text(), re.M)
        return m.group(1).strip().strip('"').strip("'") if m else default
    except Exception:
        return default


NVIDIA_KEY = secret("NVIDIA_API_KEY")
FER_JID    = secret("FER_WHATSAPP_JID")  # ← SANITIZAR: el número vive en secrets.env


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        USER_LOGS.mkdir(parents=True, exist_ok=True)
        with open(CONC_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fer_jid_from_db() -> str | None:
    """Si FER_WHATSAPP_JID no está en secrets, intenta sacarlo de kira_authz_contacts."""
    db = secret("NUBEX_DB_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db); cur = c.cursor()
        cur.execute("SELECT jid FROM kira_authz_contacts WHERE alias_coloquial ILIKE 'Fer%' OR alias_coloquial ILIKE 'Fernando%' LIMIT 1")
        row = cur.fetchone()
        c.close()
        return row[0] if row else None
    except Exception as e:
        log(f"⚠ no se pudo consultar DB para JID Fer: {e}")
        return None


# ─── Prompt 0: pre-flight ─────────────────────────────────────────────────────
def preflight() -> tuple[bool, str]:
    log("══ Prompt 0 — Pre-flight checks ══")
    for p, label in [(SOUL_F, "SOUL.md"), (TOOLS_F, "TOOLS.md"),
                     (MEMORY_F, "MEMORY.md"), (CONSTIT, "CONSTITUTION.md")]:
        if not p.exists():
            log(f"  ❌ falta archivo: {label} ({p})")
            return False, f"falta {label}"
        try:
            _ = p.read_text()
        except Exception as e:
            log(f"  ❌ no se puede leer {label}: {e}")
            return False, f"no se puede leer {label}: {e}"
        log(f"  ✓ {label} legible ({p.stat().st_size}B)")
    try:
        with urllib.request.urlopen(GATEWAY_HEALTH, timeout=5) as r:
            if r.status != 200:
                log(f"  ❌ gateway HTTP {r.status}")
                return False, f"gateway HTTP {r.status}"
    except Exception as e:
        log(f"  ❌ gateway inalcanzable: {e}")
        return False, f"gateway: {e}"
    log("  ✓ gateway /health → HTTP 200")
    log("  ✅ pre-flight OK")
    return True, ""


# ─── data collection ──────────────────────────────────────────────────────────
ERROR_RE = re.compile(r"error|timeout|failed|violated|ignored|hallucin", re.I)

def grep_log_errors() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    candidates: list[Path] = []
    if RUNTIME_L.exists():
        candidates += sorted(RUNTIME_L.glob("openclaw-*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]
    if USER_LOGS.exists():
        candidates += sorted(USER_LOGS.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]
    for p in candidates:
        try:
            with open(p, errors="ignore") as f:
                tail = f.readlines()[-3000:]
            for ln in tail:
                if ERROR_RE.search(ln):
                    hits.append((p.name, ln.strip()[:400]))
                    if len(hits) >= MAX_LOG_HITS:
                        return hits
        except Exception:
            continue
    return hits


def fmt_errors(errors: list[tuple[str, str]]) -> str:
    return "\n".join(f"  [{f}] {ln}" for f, ln in errors[:200]) or "(sin errores destacados)"


# ─── LLM (NVIDIA primary + Ollama fallback) ───────────────────────────────────
def call_nvidia(messages: list[dict], max_tokens: int, out_path: Path | None = None) -> str:
    if not NVIDIA_KEY:
        raise RuntimeError("NVIDIA_API_KEY ausente en secrets.env")
    body = json.dumps({
        "model": NVIDIA_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "stream": True,
    }).encode()
    req = urllib.request.Request(NVIDIA_BASE, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NVIDIA_KEY}",
        "Accept": "text/event-stream",
    })
    full: list[str] = []
    last_log_at = 0
    fh = open(out_path, "w") if out_path else None
    try:
        with urllib.request.urlopen(req, timeout=NVIDIA_TIMEOUT_SEC) as r:
            for raw in r:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                tok = (choices[0].get("delta", {}) or {}).get("content", "") or ""
                if tok:
                    full.append(tok)
                    if fh:
                        fh.write(tok); fh.flush()
                    size = sum(len(x) for x in full)
                    if size - last_log_at > 4000:
                        log(f"    …NVIDIA streaming {size} chars")
                        last_log_at = size
                if choices[0].get("finish_reason"):
                    break
    finally:
        if fh:
            fh.close()
    return "".join(full)


def messages_to_prompt(messages: list[dict]) -> str:
    """Aplana messages a un prompt string para Ollama /api/generate."""
    parts = []
    for m in messages:
        role = m.get("role", "user").upper()
        parts.append(f"<<{role}>>\n{m['content']}")
    parts.append("<<ASSISTANT>>\n")
    return "\n\n".join(parts)


def call_ollama(messages: list[dict], max_tokens: int, out_path: Path | None = None) -> str:
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": messages_to_prompt(messages),
        "stream": True,
        "keep_alive": "30m",
        "options": {
            "num_predict": max_tokens,
            "num_ctx": NUM_CTX_OLLAMA,
            "temperature": TEMPERATURE,
        },
    }).encode()
    req = urllib.request.Request(OLLAMA_GENERATE, data=body,
                                 headers={"Content-Type": "application/json"})
    full: list[str] = []
    last_log_at = 0
    fh = open(out_path, "w") if out_path else None
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SEC) as r:
            for raw in r:
                if not raw.strip():
                    continue
                try:
                    chunk = json.loads(raw)
                except Exception:
                    continue
                tok = chunk.get("response", "")
                if tok:
                    full.append(tok)
                    if fh:
                        fh.write(tok); fh.flush()
                    size = sum(len(x) for x in full)
                    if size - last_log_at > 4000:
                        log(f"    …Ollama streaming {size} chars")
                        last_log_at = size
                if chunk.get("done"):
                    break
    finally:
        if fh:
            fh.close()
    return "".join(full)


def call_llm(messages: list[dict], max_tokens: int, out_path: Path | None,
             label: str) -> tuple[str, str, float]:
    """Primary NVIDIA → fallback Ollama. Hasta 3 reintentos. Devuelve (texto, provider, segundos)."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES_PER_PROMPT + 1):
        t0 = time.time()
        if NVIDIA_KEY:
            try:
                log(f"  [{label}] intento {attempt} — NVIDIA Mistral…")
                out = call_nvidia(messages, max_tokens, out_path)
                if out.strip():
                    el = time.time() - t0
                    log(f"  [{label}] ✅ NVIDIA OK · {len(out)} chars · {el:.1f}s")
                    return out, "nvidia", el
                raise RuntimeError("respuesta vacía de NVIDIA")
            except Exception as e:
                last_err = e
                log(f"  [{label}] ⚠ NVIDIA falló: {type(e).__name__}: {str(e)[:140]}")
        try:
            log(f"  [{label}] intento {attempt} — Ollama fallback…")
            out = call_ollama(messages, max_tokens, out_path)
            if out.strip():
                el = time.time() - t0
                log(f"  [{label}] ✅ Ollama OK · {len(out)} chars · {el:.1f}s")
                return out, "ollama", el
            raise RuntimeError("respuesta vacía de Ollama")
        except Exception as e:
            last_err = e
            log(f"  [{label}] ⚠ Ollama falló: {type(e).__name__}: {str(e)[:140]}")
    raise RuntimeError(f"[{label}] LLM falló tras {MAX_RETRIES_PER_PROMPT} intentos: {last_err}")


# ─── builders de prompts ──────────────────────────────────────────────────────
def prompt_diagnostic(constit: str, soul: str, tools_: str, memory: str,
                      errors: list[tuple[str, str]]) -> list[dict]:
    system = ("Eres Conciencia, auditor de archivos de instrucción para agente AI.\n"
              "IDIOMA OBLIGATORIO: español. Prohibido inglés en cualquier parte del output.\n"
              "Analiza los 3 archivos con la CONSTITUTION como referencia absoluta.")
    user = f"""CONSTITUTION: {constit}

SOUL.md actual ({len(soul)} chars): {soul}

TOOLS.md actual ({len(tools_)} chars): {tools_}

MEMORY.md actual ({len(memory)} chars): {memory}

ERRORES EN LOGS (últimas 24h): {fmt_errors(errors)}

TAREA: Analiza únicamente. Devuelve DIAGNOSTICO.md en español con:
1. Qué se repite entre los 3 archivos (duplicaciones cruzadas con evidencia).
2. Qué contenido está en el archivo equivocado según CONSTITUTION.
3. Qué instrucciones son contradictorias (entre archivos o internas).
4. Qué evidencia de fallas hay en los logs y a qué regla apuntan.

Formato: secciones claras, una por punto. Sin saludos ni cierres.
NO propongas archivos optimizados — solo el diagnóstico.
"""
    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


def prompt_soul(constit: str, soul_actual: str, diagnostico: str) -> list[dict]:
    system = ("Genera el archivo directamente. PROHIBIDO comenzar con preámbulos como "
              "\"Aquí está...\", \"A continuación...\", \"El siguiente archivo...\". "
              "La primera línea debe ser el contenido del archivo.\n\n"
              "Eres Conciencia. Genera el nuevo SOUL.md.\n"
              "IDIOMA OBLIGATORIO: español. Prohibido inglés — ni una palabra.\n"
              "SOUL.md contiene SOLO: identidad, tono, reglas de comportamiento en formato TRIGGER→ACCIÓN, "
              "límites absolutos.\n"
              "NO incluyas: comandos técnicos, rutas, URLs, IPs, código, curl, tabla de modelos.\n"
              "Referencias a otros archivos en vez de repetir: \"ver TOOLS.md#seccion\".\n"
              "Formato eficiente: TRIGGER → ACCIÓN. Sin prosa narrativa. Sin headers decorativos.")
    user = f"""CONSTITUTION (sección SOUL): {constit}

SOUL.md actual ({len(soul_actual)} chars): {soul_actual}

DIAGNOSTICO previo: {diagnostico}

TAREA: Devuelve el SOUL.md optimizado, completo, en español.
SIN markdown code fences (```markdown ... ```). Solo el contenido del archivo.
"""
    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


def prompt_memory(constit: str, memory_actual: str, diagnostico: str) -> list[dict]:
    system = ("Genera el archivo directamente. PROHIBIDO comenzar con preámbulos como "
              "\"Aquí está...\", \"A continuación...\", \"El siguiente archivo...\". "
              "La primera línea debe ser el contenido del archivo.\n\n"
              "Eres Conciencia. Genera el nuevo MEMORY.md.\n"
              "IDIOMA OBLIGATORIO: español. Prohibido inglés.\n"
              "MEMORY.md contiene SOLO: perfil fijo del usuario, decisiones técnicas permanentes con fecha, "
              "aprendizajes validados con evidencia, errores recurrentes con solución.\n"
              "NO incluyas: infraestructura técnica detallada, reglas de comportamiento, "
              "información provisional, logs temporales.\n"
              "Secciones [FIJO] claramente marcadas. Aprendizajes en formato "
              "[YYYY-MM-DD] descripción → solución.")
    user = f"""CONSTITUTION (sección MEMORY): {constit}

MEMORY.md actual ({len(memory_actual)} chars): {memory_actual}

DIAGNOSTICO previo: {diagnostico}

TAREA: Devuelve el MEMORY.md optimizado, completo, en español.
SIN code fences. Solo el contenido del archivo.
"""
    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


def prompt_tools(constit: str, tools_actual: str, diagnostico: str) -> list[dict]:
    system = ("Genera el archivo directamente. PROHIBIDO comenzar con preámbulos como "
              "\"Aquí está...\", \"A continuación...\", \"El siguiente archivo...\". "
              "La primera línea debe ser el contenido del archivo.\n\n"
              "CRÍTICO: NUNCA inventes endpoints, rutas, comandos o paths. Solo documenta lo que "
              "realmente existe en el sistema según los archivos actuales. Si no estás seguro si "
              "algo existe, omítelo.\n\n"
              "Eres Conciencia. Genera el nuevo TOOLS.md.\n"
              "IDIOMA OBLIGATORIO: español. Prohibido inglés.\n"
              "TOOLS.md contiene SOLO: endpoints, comandos exactos, rutas de acceso, "
              "tabla de modelos disponibles.\n"
              "NO incluyas: reglas de comportamiento, historia, aprendizajes, información que ya está "
              "en MEMORY.md o SOUL.md.\n"
              "Referencias cruzadas en vez de repetir contenido.\n\n"
              "CRÍTICO: TOOLS.md debe documentar TODOS los comandos que SOUL.md referencia.\n"
              "Antes de finalizar, verifica que estos comandos tienen sección en TOOLS.md:\n"
              "- /mail y triage.py\n"
              "- process poll/kill\n"
              "- quien-es (personas-context)\n"
              "- [agrega aquí tus comandos]\n"
              "Si falta alguno, agrégalo antes de terminar.")
    user = f"""CONSTITUTION (sección TOOLS): {constit}

TOOLS.md actual ({len(tools_actual)} chars): {tools_actual}

DIAGNOSTICO previo: {diagnostico}

TAREA: Devuelve el TOOLS.md optimizado, completo, en español.
SIN code fences. Solo el contenido del archivo.
"""
    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


def prompt_validation(constit: str, soul_p: str, tools_p: str, memory_p: str) -> list[dict]:
    system = ("Eres Conciencia. Valida que los 3 archivos propuestos son coherentes entre sí.\n"
              "IDIOMA OBLIGATORIO: español.\n"
              "Verifica: sin duplicaciones entre archivos, sin contradicciones, "
              "sin información crítica perdida vs los originales, "
              "cada archivo respeta su rol según CONSTITUTION.\n\n"
              "Tu respuesta DEBE comenzar exactamente con una de estas dos palabras en la primera línea:\n"
              "APROBADO\n"
              "RECHAZADO\n"
              "Luego el análisis. Sin títulos, sin markdown antes del veredicto.")
    user = f"""CONSTITUTION: {constit}

SOUL.md propuesto ({len(soul_p)} chars): {soul_p}

TOOLS.md propuesto ({len(tools_p)} chars): {tools_p}

MEMORY.md propuesto ({len(memory_p)} chars): {memory_p}

TAREA: Devuelve VALIDACION.md con:
- PRIMERA LÍNEA exactamente APROBADO o RECHAZADO (mayúsculas, una sola palabra).
- Si APROBADO: breve justificación de por qué los 3 archivos cumplen su rol.
- Si RECHAZADO: lista detallada de problemas + recomendaciones específicas por archivo.

La primera línea es lo que decide si se aplica a producción — sé estricto pero justo.
"""
    return [{"role": "system", "content": system},
            {"role": "user",   "content": user}]


# ─── backup / apply / rollback ────────────────────────────────────────────────
def backup_prod(today: str) -> Path:
    dest = BACKUPS / today
    dest.mkdir(parents=True, exist_ok=True)
    for src in (SOUL_F, TOOLS_F, MEMORY_F):
        shutil.copy2(src, dest / src.name)
    # rotación: conservar últimos 5
    all_b = sorted([p for p in BACKUPS.iterdir() if p.is_dir() and not p.name.startswith("pre-rollback")])
    for old in all_b[:-MAX_BACKUPS]:
        try:
            shutil.rmtree(old)
            log(f"  backup viejo borrado: {old.name}")
        except Exception:
            pass
    return dest


def apply_files(out_dir: Path) -> None:
    for name in ("SOUL.md", "TOOLS.md", "MEMORY.md"):
        src = out_dir / name
        if not src.exists():
            raise FileNotFoundError(f"propuesta faltante: {src}")
        shutil.copy2(src, WORKSPACE / name)


def reload_gateway() -> bool:
    log("  recargando gateway…")
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.openclaw.gateway"],
                   capture_output=True, timeout=30)
    time.sleep(10)
    try:
        with urllib.request.urlopen(GATEWAY_HEALTH, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def rollback_to(date_str: str) -> int:
    src = BACKUPS / date_str
    if not src.exists():
        log(f"❌ backup {date_str} no existe en {BACKUPS}")
        return 1
    safety = BACKUPS / f"pre-rollback-{datetime.datetime.now():%Y%m%d_%H%M%S}"
    safety.mkdir(parents=True, exist_ok=True)
    for s in (SOUL_F, TOOLS_F, MEMORY_F):
        shutil.copy2(s, safety / s.name)
    log(f"safety backup del estado actual → {safety}")
    restaurados = 0
    for name in ("SOUL.md", "TOOLS.md", "MEMORY.md"):
        f = src / name
        if f.exists():
            shutil.copy2(f, WORKSPACE / name)
            log(f"  restaurado {name} desde {date_str}")
            restaurados += 1
    if not restaurados:
        log(f"❌ backup {date_str} no contiene archivos")
        return 1
    ok = reload_gateway()
    log(f"gateway tras rollback: {'sano ✅' if ok else '⚠ no sano'}")
    return 0 if ok else 2


# ─── notificación ─────────────────────────────────────────────────────────────
def send_wa(msg: str) -> None:
    target = FER_JID or fer_jid_from_db()
    if not target:
        log("  ⚠ sin destinatario de notificación (FER_WHATSAPP_JID en secrets.env); se omite")
        return
    cmd = [OPENCLAW, "message", "send", "--channel", "whatsapp",
           "--target", target, "--message", msg[:3500]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        log(f"  WA send rc={r.returncode} → {target}")
    except Exception as e:
        log(f"  ⚠ WA send falló: {e}")


# ─── status ───────────────────────────────────────────────────────────────────
def cmd_status() -> int:
    if not PROPUESTAS.exists():
        print("(sin propuestas)"); return 0
    dirs = sorted([p for p in PROPUESTAS.iterdir() if p.is_dir()], reverse=True)
    if not dirs:
        print("(sin propuestas)"); return 0
    latest = dirs[0]
    print(f"📋 Última propuesta: {latest.name}")
    for fn in ("DIAGNOSTICO.md", "SOUL.md", "TOOLS.md", "MEMORY.md", "VALIDACION.md"):
        f = latest / fn
        if f.exists():
            print(f"  {fn:18} {f.stat().st_size:>7} B")
        else:
            print(f"  {fn:18} (no existe)")
    print()
    if BACKUPS.exists():
        bs = sorted([p for p in BACKUPS.iterdir() if p.is_dir()], reverse=True)[:5]
        print(f"💾 Backups disponibles ({len(bs)} máx):")
        for p in bs:
            print(f"  {p.name}")
    print()
    print(f"📜 Otras propuestas históricas ({len(dirs)-1}):")
    for p in dirs[1:6]:
        print(f"  {p.name}")
    return 0


# ─── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Conciencia — curador de SOUL/TOOLS/MEMORY")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run",  action="store_true",   help="Prompts 0-5, propone, NO aplica")
    g.add_argument("--apply",    action="store_true",   help="Prompts 0-6, aplica si validación aprueba")
    g.add_argument("--rollback", metavar="YYYY-MM-DD", help="Restaura backup de esa fecha")
    g.add_argument("--status",   action="store_true",   help="Muestra última propuesta + backups")
    a = ap.parse_args()

    if a.status:
        return cmd_status()
    if a.rollback:
        log(f"════ Rollback a {a.rollback} ════")
        return rollback_to(a.rollback)

    today = datetime.date.today().isoformat()
    mode  = "APPLY" if a.apply else "DRY-RUN"
    log(f"════════════ Conciencia {today} [{mode}] ════════════")

    # ── Prompt 0 ──
    ok, why = preflight()
    if not ok:
        send_wa(f"❌ Conciencia abortó en pre-flight: {why}")
        return 1

    # cargar datos
    soul   = SOUL_F.read_text()
    tools_ = TOOLS_F.read_text()
    memory = MEMORY_F.read_text()
    constit = CONSTIT.read_text()
    errors = grep_log_errors()
    log(f"datos cargados: SOUL {len(soul)}c · TOOLS {len(tools_)}c · MEMORY {len(memory)}c · "
        f"{len(errors)} líneas con error/timeout/failed/violated/ignored/hallucin")

    out_dir = PROPUESTAS / today
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, tuple[str, int, int, float]] = {}

    # ── helper de resume ──
    RESUME_MIN_BYTES = 300
    def maybe_resume(filename: str, label: str) -> str | None:
        """Si el archivo ya existe y supera el umbral, devuelve su contenido (skip LLM)."""
        p = out_dir / filename
        if p.exists() and p.stat().st_size > RESUME_MIN_BYTES:
            c = p.read_text()
            log(f"  ♻️  [{label}] resume: {filename} ya existe ({len(c)}c) — skip LLM")
            return c
        return None

    try:
        # ── Prompt 1 ──
        log("══ Prompt 1 — DIAGNOSTICO global ══")
        diagnostico = maybe_resume("DIAGNOSTICO.md", "P1")
        if diagnostico is None:
            msgs = prompt_diagnostic(constit, soul, tools_, memory, errors)
            inp = sum(len(m["content"]) for m in msgs)
            diagnostico, prov, el = call_llm(msgs, MAX_TOKENS_DIAG,
                                             out_dir / "DIAGNOSTICO.md", "P1")
            metrics["P1"] = (prov, inp, len(diagnostico), el)
        else:
            metrics["P1"] = ("resume", 0, len(diagnostico), 0.0)

        # ── Prompt 2 ──
        log("══ Prompt 2 — generar SOUL.md ══")
        soul_new = maybe_resume("SOUL.md", "P2")
        if soul_new is None:
            msgs = prompt_soul(constit, soul, diagnostico)
            inp = sum(len(m["content"]) for m in msgs)
            soul_new, prov, el = call_llm(msgs, MAX_TOKENS_FILE,
                                          out_dir / "SOUL.md", "P2")
            metrics["P2"] = (prov, inp, len(soul_new), el)
        else:
            metrics["P2"] = ("resume", 0, len(soul_new), 0.0)

        # ── Prompt 3 ──
        log("══ Prompt 3 — generar MEMORY.md ══")
        memory_new = maybe_resume("MEMORY.md", "P3")
        if memory_new is None:
            msgs = prompt_memory(constit, memory, diagnostico)
            inp = sum(len(m["content"]) for m in msgs)
            memory_new, prov, el = call_llm(msgs, MAX_TOKENS_FILE,
                                            out_dir / "MEMORY.md", "P3")
            metrics["P3"] = (prov, inp, len(memory_new), el)
        else:
            metrics["P3"] = ("resume", 0, len(memory_new), 0.0)

        # ── Prompt 4 ──
        log("══ Prompt 4 — generar TOOLS.md ══")
        tools_new = maybe_resume("TOOLS.md", "P4")
        if tools_new is None:
            msgs = prompt_tools(constit, tools_, diagnostico)
            inp = sum(len(m["content"]) for m in msgs)
            tools_new, prov, el = call_llm(msgs, MAX_TOKENS_FILE,
                                           out_dir / "TOOLS.md", "P4")
            metrics["P4"] = (prov, inp, len(tools_new), el)
        else:
            metrics["P4"] = ("resume", 0, len(tools_new), 0.0)

        # ── Prompt 5 ──
        log("══ Prompt 5 — VALIDACION cruzada ══")
        validacion = maybe_resume("VALIDACION.md", "P5")
        if validacion is None:
            msgs = prompt_validation(constit, soul_new, tools_new, memory_new)
            inp = sum(len(m["content"]) for m in msgs)
            validacion, prov, el = call_llm(msgs, MAX_TOKENS_VALID,
                                            out_dir / "VALIDACION.md", "P5")
            metrics["P5"] = (prov, inp, len(validacion), el)
        else:
            metrics["P5"] = ("resume", 0, len(validacion), 0.0)

        # buscar APROBADO/RECHAZADO en las primeras 10 líneas
        aprobado = False
        veredicto_line = ""
        for ln in validacion.strip().splitlines()[:10]:
            up = ln.strip().upper()
            clean = re.sub(r"[*#`>\-_]+", "", up).strip()
            if clean.startswith("APROBADO"):
                aprobado = True; veredicto_line = ln.strip(); break
            if clean.startswith("RECHAZADO"):
                aprobado = False; veredicto_line = ln.strip(); break
        if not veredicto_line:
            veredicto_line = validacion.strip().splitlines()[0][:60] if validacion.strip() else "(vacío)"
        log(f"VALIDACION dice: {veredicto_line[:60]} → {'APROBADO ✅' if aprobado else 'RECHAZADO ❌'}")

    except Exception as e:
        log(f"❌ flujo abortó: {e}")
        send_wa(f"❌ Conciencia {today} abortó: {str(e)[:200]}")
        return 2

    # ── resumen de métricas ──
    log("───── métricas por prompt ─────")
    total_time = 0.0
    for k, (p, i, o, el) in metrics.items():
        log(f"  {k}  prov={p:7}  input={i:>7}c  output={o:>6}c  tiempo={el:>6.1f}s")
        total_time += el
    log(f"  TOTAL: {total_time:.1f}s ({total_time/60:.1f} min)")

    # ── Prompt 6 (solo --apply y APROBADO) ──
    if a.apply:
        if not aprobado:
            log("❌ VALIDACION rechazó — no se aplica a producción")
            send_wa(f"❌ Conciencia {today}: VALIDACION RECHAZADO. Propuesta en {out_dir}. "
                    f"Revisa antes de aplicar manual.")
            return 3
        log("══ Prompt 6 — Aplicando a producción ══")
        b = backup_prod(today)
        log(f"  backup producción creado en {b}")
        try:
            apply_files(out_dir)
            log("  archivos reemplazados en producción")
        except Exception as e:
            log(f"  ❌ apply falló: {e} — restaurando del backup")
            for s in (SOUL_F, TOOLS_F, MEMORY_F):
                shutil.copy2(b / s.name, s)
            reload_gateway()
            send_wa(f"❌ Conciencia: apply falló ({str(e)[:120]}). Restaurado al estado previo.")
            return 4
        if not reload_gateway():
            log("  ⚠ gateway no respondió tras apply — rollback automático")
            for s in (SOUL_F, TOOLS_F, MEMORY_F):
                shutil.copy2(b / s.name, s)
            reload_gateway()
            send_wa(f"⚠ Conciencia {today}: gateway no sano tras apply. ROLLBACK automático al backup.")
            return 5
        # éxito
        sa = (WORKSPACE / "SOUL.md").stat().st_size
        ta = (WORKSPACE / "TOOLS.md").stat().st_size
        ma = (WORKSPACE / "MEMORY.md").stat().st_size
        msg = (f"Conciencia aplicada ✅ SOUL/TOOLS/MEMORY optimizados. "
               f"Tamaños: SOUL {len(soul)//1000}k→{sa//1000}k, "
               f"TOOLS {len(tools_)//1000}k→{ta//1000}k, "
               f"MEMORY {len(memory)//1000}k→{ma//1000}k. "
               f"Backup en {b.name}.")
        send_wa(msg)
        log(f"  ✅ aplicación exitosa")
        return 0

    # dry-run → terminó
    log(f"DRY-RUN completado. Propuestas en {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

