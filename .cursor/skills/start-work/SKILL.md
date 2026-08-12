---
name: start-work
description: >-
  Arranque operativo del proyecto Movistar Hubox. Usar cuando el usuario diga
  "start work", "empieza", "arranca", "status", "prendelo" o quiera retomar
  sesión de producción. Verifica bot Telegram, pool de perfiles, último enroll
  y deja el sistema listo para vinculaciones.
---

# Start Work — Movistar Hubox

## Objetivo

Al invocarse, **no preguntes qué hacer**: ejecuta el checklist, reporta estado y corrige lo obvio (bot caído, procesos duplicados).

Documento maestro: `.cursor/docs/FLUJO-MOVISTAR-CRONOLOGICO.md`

## Checklist (en orden)

### 1. Bot Telegram

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*telegram_bot*' }
```

- **0 procesos** → arrancar: `python telegram_bot.py` (background, cwd = raíz del repo)
- **>1 procesos** → matar todos y reiniciar uno solo
- Confirmar en logs: `Application started` y `Bot arrancando`

Token y admins: `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`)

### 2. Pool de perfiles

```bash
python -c "from profile_pool import startup_report, load_profiles; print(startup_report()); print([(p.id,p.label) for p in load_profiles()])"
```

Reportar: activos / descartados / éxitos por perfil (máx 10 c/u).

Pool activo = carpetas **numéricas puras** en `profiles/movistar_perfiles/` (`1`, `2`, …).

### 3. Última actividad

- `.hubox_state.json` → último enroll (teléfono, paso, éxito/error)
- `bot_data.json` → créditos recientes / vinculaciones del día
- `profile_usage.json` → contadores por perfil

### 4. Salud rápida (solo si algo falla)

```bash
python validate_profiles.py
```

Errores frecuentes ya resueltos en este repo:
- `DECODE_NO_TXT` → `_build_biograficos()` debe usar `nombre_pila`, `apellido_*`, `vigencia`
- INE antigua (solo QR URL en reverso) → **no apta** para flujo GH actual
- Perfiles GH aptos requieren 2 QR binarios en reverso

## Formato de respuesta al usuario

```
## Estado operativo
- Bot: ✅/❌ (PID, token parcial)
- Pool: N activos — listar id + nombre + éxitos/10
- Último enroll: teléfono, resultado, timestamp

## Acciones tomadas
(solo si reiniciaste bot, limpiaste duplicados, etc.)

## Pendiente / riesgos
(perfiles bajos, INE viejas sin importar, errores en logs)
```

Mantén el reporte corto. No importes perfiles ni cambies `.env` salvo que el usuario lo pida.

## Comandos útiles

| Acción | Comando |
|--------|---------|
| Reiniciar bot | matar `*telegram_bot*` → `python telegram_bot.py` |
| Validar pool | `python validate_profiles.py` |
| Preparar OCR | `/preparar` en bot o `prepare_all()` |
| Smoke API | `python enroll_replay.py inicio --phone <10dig>` |
| Presencial (sin OTP) | `movistar_presencial.py` — ver `.cursor/docs/FLUJO-PRESENCIAL-CREDENCIALES.md` |

## Reglas

- **No commitear** `.env`, tokens ni credenciales Hubox.
- **No tocar** `profiles/movistar_perfiles/` sin pedido explícito.
- Tras cambios en `enroll_automation.py` o pool → **reiniciar bot**.
- Para migrar/clonar repo → delegar al subagente `movistar-flow-migrator`.
