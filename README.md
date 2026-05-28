# Conciencia — Curador nocturno de archivos de instrucción para agentes de IA

> Un curador nocturno que mantiene sanos los archivos de instrucción de un agente de IA (SOUL.md, TOOLS.md, MEMORY.md) — los audita, propone versiones limpias y las aplica solo con aprobación, guardando respaldo de todo.

Esta es una guía completa para entender el patrón y replicarlo. Incluye los prompts exactos y el script completo, tal como corre en producción.

---

## Tabla de contenido

1. [El problema](#1-el-problema-drift-de-los-archivos-de-instrucción)
2. [Principios de diseño](#2-principios-de-diseño)
3. [Requisitos previos](#3-requisitos-previos)
4. [Estructura de archivos](#4-estructura-de-archivos)
5. [La constitución](#5-la-constitución-constitutionmd)
6. [El flujo de 6 pasos, prompt por prompt](#6-el-flujo-de-6-pasos-prompt-por-prompt)
7. [Detalles de implementación](#7-detalles-de-implementación)
8. [Cómo correrlo](#8-cómo-correrlo)
9. [Programarlo (scheduler)](#9-programarlo-scheduler)
10. [El script completo](#10-el-script-completo-conciencispy)
11. [Antes de publicar: sanitización](#11-antes-de-publicar-sanitización)
12. [Decisiones de diseño honestas](#12-decisiones-de-diseño-honestas)
13. [Mejoras conocidas](#13-mejoras-conocidas)

---

## 1. El problema: drift de los archivos de instrucción

Muchos agentes modernos (OpenClaw entre ellos) no viven en una base de datos ni en un panel. Viven en archivos de texto plano que el agente lee al inicio de cada sesión y que se inyectan en su contexto. Los archivos son el agente:

- **SOUL.md** — identidad, tono, reglas de comportamiento, límites.
- **TOOLS.md** — herramientas, endpoints, comandos, rutas.
- **MEMORY.md** — perfil del usuario, decisiones permanentes, aprendizajes.

Es elegante, pero a las semanas de uso aparece el deterioro:

- **Duplicación** — la misma regla escrita en dos o tres archivos; cada copia ocupa contexto y puede divergir.
- **Contradicción** — una instrucción nueva contradice a una vieja que nadie borró; el agente oscila.
- **Archivo equivocado** — reglas de comportamiento que terminan en TOOLS.md, comandos que terminan en MEMORY.md.
- **Acumulación** — cada edición agrega, casi ninguna quita; los archivos crecen y se vuelven caros de inyectar.

El efecto es peor con modelos chicos o locales (Qwen, variantes "flash"/"mini"): toleran mucho menos las instrucciones vagas o contradictorias y hacen drift en cuanto la especificación tiene huecos.

---

## 2. Principios de diseño

Conciencia se diseñó sobre cuatro principios:

1. **Constitución como ancla.** Un archivo CONSTITUTION.md separado e inmutable define qué debe contener cada archivo y cómo escribirlo. Conciencia optimiza contra la constitución, no contra su propio criterio.
2. **Focalización.** Varios prompts pequeños: primero diagnostica, luego regenera un archivo a la vez, y al final valida los tres juntos.
3. **Reversibilidad por defecto.** Respaldo fechado antes de aplicar, rotación de los últimos 5, y rollback automático si el agente no responde sano.
4. **Humano en el lazo.** Conciencia propone; no aplica nada sin aprobación.

---

## 3. Requisitos previos

- Un agente cuyo estado viva en archivos editables (SOUL.md, TOOLS.md, MEMORY.md).
- Un `CONSTITUTION.md` que tú escribes una vez.
- Dos LLMs:
  - Primario: un modelo fuerte vía API (en la referencia: Mistral Large 3 vía NVIDIA NIM).
  - Fallback: un modelo local vía Ollama (en la referencia: qwen3.6:35b-a3b).
- Un archivo de secretos (`~/.openclaw/secrets.env`) con la API key.
- Python 3.10+.

| Parámetro | Valor en la referencia |
|---|---|
| Modelo primario | mistralai/mistral-large-3-675b-instruct-2512 (NVIDIA NIM) |
| Endpoint primario | https://integrate.api.nvidia.com/v1/chat/completions |
| Timeout primario | 600 s |
| Modelo fallback | qwen3.6:35b-a3b (Ollama /api/generate) |
| Timeout fallback | 1800 s |
| num_ctx Ollama | 200000 |
| Temperature | 0.3 |
| Reintentos por prompt | 3 |
| Respaldos retenidos | 5 |

---

## 4. Estructura de archivos

```
~/.openclaw/workspace/
├── SOUL.md                       ← producción
├── TOOLS.md                      ← producción
├── MEMORY.md                     ← producción
└── _conciencia/
    ├── CONSTITUTION.md           ← reglas inmutables (tú las escribes)
    ├── propuestas/
    │   └── YYYY-MM-DD/
    │       ├── DIAGNOSTICO.md
    │       ├── SOUL.md
    │       ├── MEMORY.md
    │       ├── TOOLS.md
    │       └── VALIDACION.md
    └── backups/
        └── YYYY-MM-DD/
            ├── SOUL.md
            ├── TOOLS.md
            └── MEMORY.md

~/.openclaw/
├── secrets.env                   ← API keys (fuera de git)
└── logs/conciencia.log

~/.openclaw/workspace/bin/conciencia.py
```

---

## 5. La constitución (CONSTITUTION.md)

Es el corazón del patrón. Conciencia nunca la modifica; la usa como referencia absoluta. Estructura mínima:

```markdown
# CONSTITUTION

## Idioma
Todo el output debe estar en español. Prohibido inglés.

## SOUL.md
Contiene SOLO: identidad, tono, reglas de comportamiento en formato TRIGGER → ACCIÓN, límites absolutos.
NO: comandos técnicos, rutas, URLs, IPs, código, tablas de modelos.

## TOOLS.md
Contiene SOLO: endpoints, comandos exactos, rutas de acceso, modelos disponibles.
NO: reglas de comportamiento, historia, aprendizajes.

## MEMORY.md
Contiene SOLO: perfil fijo del usuario, decisiones técnicas permanentes con fecha,
aprendizajes validados con evidencia, errores recurrentes con solución.
NO: infraestructura detallada, reglas de comportamiento, info provisional.

## Reglas de escritura
- Formato eficiente. Sin prosa decorativa.
- Referenciar otros archivos en vez de repetir.
- Nunca eliminar información necesaria: reorganizar y compactar, no amputar.
```

---

## 6. El flujo de 6 pasos, prompt por prompt

Son 6 pasos en orden (Prompt 0 a Prompt 6). El Prompt 0 y el Prompt 6 son código determinista (no llaman al LLM). Los prompts que sí llaman al LLM son 5: del 1 al 5.

### Prompt 0 — Pre-flight (código, sin LLM)

Verifica que SOUL.md, TOOLS.md, MEMORY.md y CONSTITUTION.md existen y se leen, y que el gateway responde HTTP 200 en `/health`. Si algo falla → aborta.

### Prompt 1 — Diagnóstico global

Lee los 3 archivos + la constitución + las líneas de log con errores. Solo analiza; no reescribe nada. Produce `DIAGNOSTICO.md`.

**SYSTEM:**
```
Eres Conciencia, auditor de archivos de instrucción para agente AI.
IDIOMA OBLIGATORIO: español. Prohibido inglés en cualquier parte del output.
Analiza los 3 archivos con la CONSTITUTION como referencia absoluta.
```

**USER:**
```
CONSTITUTION: {constit}

SOUL.md actual ({len} chars): {soul}

TOOLS.md actual ({len} chars): {tools_}

MEMORY.md actual ({len} chars): {memory}

ERRORES EN LOGS (últimas 24h): {fmt_errors(errors)}

TAREA: Analiza únicamente. Devuelve DIAGNOSTICO.md en español con:
1. Qué se repite entre los 3 archivos (duplicaciones cruzadas con evidencia).
2. Qué contenido está en el archivo equivocado según CONSTITUTION.
3. Qué instrucciones son contradictorias (entre archivos o internas).
4. Qué evidencia de fallas hay en los logs y a qué regla apuntan.

Formato: secciones claras, una por punto. Sin saludos ni cierres.
NO propongas archivos optimizados — solo el diagnóstico.
```

### Prompt 2 — Genera SOUL.md

**SYSTEM:**
```
Genera el archivo directamente. PROHIBIDO comenzar con preámbulos.
La primera línea debe ser el contenido del archivo.

Eres Conciencia. Genera el nuevo SOUL.md.
IDIOMA OBLIGATORIO: español. Prohibido inglés.
SOUL.md contiene SOLO: identidad, tono, reglas de comportamiento en
formato TRIGGER→ACCIÓN, límites absolutos.
NO incluyas: comandos técnicos, rutas, URLs, IPs, código, curl, tabla de modelos.
```

**USER:**
```
CONSTITUTION (sección SOUL): {constit}

SOUL.md actual ({len} chars): {soul_actual}

DIAGNOSTICO previo: {diagnostico}

TAREA: Devuelve el SOUL.md optimizado, completo, en español.
SIN markdown code fences. Solo el contenido del archivo.
```

### Prompt 3 — Genera MEMORY.md

Igual que Prompt 2 pero para MEMORY.md. El system prompt instruye: `MEMORY.md contiene SOLO: perfil fijo del usuario, decisiones técnicas permanentes con fecha, aprendizajes validados con evidencia, errores recurrentes con solución.`

### Prompt 4 — Genera TOOLS.md

El más estricto: prohíbe inventar endpoints. Incluye un checklist de comandos que el archivo debe documentar (sustituye esa lista por la tuya).

**Fragmento del system:**
```
CRÍTICO: NUNCA inventes endpoints, rutas, comandos o paths.
Solo documenta lo que realmente existe en el sistema según los archivos actuales.

CRÍTICO: TOOLS.md debe documentar TODOS los comandos que SOUL.md referencia.
Antes de finalizar, verifica que estos comandos tienen sección en TOOLS.md:
- /mail y triage.py
- process poll/kill
- [... tus comandos ...]
Si falta alguno, agrégalo antes de terminar.
```

### Prompt 5 — Validación cruzada

Toma los 3 archivos nuevos juntos + la constitución y emite un veredicto. La primera línea decide si se aplica.

**SYSTEM:**
```
Eres Conciencia. Valida que los 3 archivos propuestos son coherentes entre sí.
IDIOMA OBLIGATORIO: español.
Verifica: sin duplicaciones entre archivos, sin contradicciones,
sin información crítica perdida vs los originales,
cada archivo respeta su rol según CONSTITUTION.

Tu respuesta DEBE comenzar exactamente con una de estas dos palabras en la primera línea:
APROBADO
RECHAZADO
```

### Prompt 6 — Aplicación (código, sin LLM)

Solo con `--apply` y solo si la validación empezó con `APROBADO`:

1. Respalda los 3 archivos de producción.
2. Reemplaza los archivos de producción con las propuestas.
3. Recarga el gateway y verifica `/health`.
4. Si falla → rollback automático + notifica.

---

## 7. Detalles de implementación

- **LLM con fallback en cadena.** Cada prompt intenta el primario (NVIDIA, streaming SSE) hasta 3 veces; si falla, cae al fallback local (Ollama).
- **Streaming a disco.** La respuesta se escribe token por token al archivo de salida.
- **Resume.** `maybe_resume()` revisa si el archivo de salida ya existe y supera 300 bytes; si sí, lo reutiliza y salta la llamada al LLM.
- **Parsing del veredicto.** Busca APROBADO/RECHAZADO en las primeras 10 líneas, limpiando markdown.
- **Rotación de respaldos.** Conserva los últimos 5; borra el más viejo.
- **Notificación.** Vía `openclaw message send --channel whatsapp`.

---

## 8. Cómo correrlo

```bash
# 1) SIEMPRE la primera vez: analiza y propone sin tocar nada
conciencia.py --dry-run

# 2) Revisa a mano las propuestas
cat ~/.openclaw/workspace/_conciencia/propuestas/$(date +%F)/DIAGNOSTICO.md
cat ~/.openclaw/workspace/_conciencia/propuestas/$(date +%F)/VALIDACION.md

# 3) Ver estado de propuestas y respaldos
conciencia.py --status

# 4) Solo cuando confíes: aplica (solo si VALIDACION = APROBADO)
conciencia.py --apply

# 5) Si algo salió mal: regresa a un respaldo
conciencia.py --rollback 2026-05-28
```

---

## 9. Programarlo (scheduler)

**macOS — LaunchAgent** (`~/Library/LaunchAgents/ai.openclaw.conciencia.plist`), domingo 2:00 a.m.:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.openclaw.conciencia</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/USUARIO/.openclaw/workspace/bin/conciencia.py</string>
    <string>--apply</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>0</integer>
        <key>Hour</key><integer>2</integer>
        <key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/USUARIO/.openclaw/logs/conciencia.log</string>
  <key>StandardErrorPath</key><string>/Users/USUARIO/.openclaw/logs/conciencia.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/ai.openclaw.conciencia.plist
```

**Linux — cron:**

```bash
0 2 * * 0 /usr/bin/python3 ~/.openclaw/workspace/bin/conciencia.py --apply >> ~/.openclaw/logs/conciencia.log 2>&1
```

> Recomendación: programa `--dry-run` las primeras semanas y aplica a mano, hasta que confíes en las propuestas.

---

## 10. El script completo (conciencia.py)

Ver el archivo [`conciencia.py`](conciencia.py) en este repositorio.

Lo que modifica respecto a producción (tokens PII/secretos):
- La IP de Ollama → usa variable `OLLAMA_HOST` o pon la tuya.
- El número de WhatsApp → vive en `secrets.env` como `FER_WHATSAPP_JID` (cámbialo por tu identificador).
- El nombre "Fer" en el Prompt 3 → cámbialo a "del usuario" si quieres anonimato.

---

## 11. Antes de publicar: sanitización

Antes del `git push`, corre un escáner de secretos (TruffleHog, gitleaks) como pre-push hook.

| Token | Dónde aparece | Sensibilidad |
|---|---|---|
| "perfil fijo del usuario" | Prompt 3 (MEMORY) | Tu nombre. Genericea si quieres anonimato. |
| Lista de comandos internos | Prompt 4 (TOOLS) | Revela tu arquitectura. Sustituye por los tuyos. |
| kira_authz_contacts, NUBEX_DB_URL | fer_jid_from_db() | Nombres de tu BD/negocio. Genericea o borra. |

**Secretos — solo en secrets.env, nunca en el repo:** `NVIDIA_API_KEY`, `FER_WHATSAPP_JID`, `NUBEX_DB_URL`.

**.gitignore mínimo:**

```
secrets.env
.openclaw/logs/
_conciencia/propuestas/
_conciencia/backups/
*.bak
```

---

## 12. Decisiones de diseño honestas

**No es un concepto inédito.** La idea de que un agente reescriba sus propios archivos de instrucción ya circula en la comunidad. Lo que aporta esta implementación es la disciplina: constitución como ancla, flujo focalizado, validación cruzada y rollback automático.

**La deduplicación NO se hace pasando archivos generados entre prompts.** Cada regeneración recibe solo su archivo actual + el diagnóstico, no las versiones nuevas de los otros. La coherencia se delega a (a) el diagnóstico inicial, (b) las reglas de rol por archivo en cada system prompt, y (c) la validación cruzada final.

**El veredicto es frágil por diseño.** Depende de que el modelo escriba APROBADO/RECHAZADO en las primeras 10 líneas. Si el modelo no respeta el formato, el `--apply` no procede (falla seguro, no aplica de más).

---

## 13. Mejoras conocidas

- **Calibración cross-model como detector de drift.** Correr el mismo diagnóstico por el modelo fuerte y el chico: donde el chico se desvía, la especificación está demasiado vaga.
- **Pasar los archivos ya generados a los prompts siguientes** para reforzar la deduplicación.
- **Bitácora de auditoría a prueba de manipulación** (hash encadenado) de qué cambió cada corrida.
- **"No silent pass"**: reportar también las corridas sanas, no solo los problemas.

---

## Créditos y licencia

Construido como herramienta interna y publicado como implementación de referencia para la comunidad de OpenClaw.

Licencia: MIT — ver [LICENSE](LICENSE).
