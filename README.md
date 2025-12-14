# Agendamiento de Citas

Aplicación en Streamlit para agendar, actualizar y cancelar citas con validaciones de horario (8am-6pm UTC-5). Envía confirmaciones por Gmail, crea eventos en Google Calendar y persiste la información en Google Sheets. Usa OAuth de usuario (no service account).

## Requisitos
- Python 3.10+
- [uv](https://github.com/astral-sh/uv) para manejar el entorno virtual
- Credenciales de Google (service account) con acceso a Calendar y Sheets
- Gmail con contraseña de aplicación para envío de correos

## Configuración
1. Copia `.env.example` a `.env` y completa:
   - `GOOGLE_OAUTH_CLIENT_FILE` (archivo `oauth_client.json` descargado desde Google Cloud) o `GOOGLE_OAUTH_CLIENT_JSON` (JSON en línea)
   - `GOOGLE_OAUTH_TOKEN_FILE` (opcional, por defecto `.streamlit/oauth_token.json`)
   - `GOOGLE_SHEETS_SPREADSHEET_ID`
   - `GOOGLE_CALENDAR_ID` (por ejemplo `primary` si tu usuario tiene acceso)
   - `GMAIL_USER` y `GMAIL_APP_PASSWORD`
   - `TZ` (por defecto `America/Bogota`)
2. Ejecuta la app una vez para que abra el flujo OAuth en el navegador; inicia sesión con el usuario que tenga permisos sobre el calendario y la hoja. El token refrescable quedará en `GOOGLE_OAUTH_TOKEN_FILE`.

## Instalación con uv
```bash
uv venv
uv sync
```

## Ejecutar la app
```bash
uv run streamlit run app.py
```

## Notas
- No se permiten citas en el pasado ni fuera de 8am-6pm (UTC-5).
- No se permiten duplicados exactos de horario mientras la cita esté activa.
- Las operaciones de actualizar y cancelar requieren que el correo tenga citas activas y usa el ID de la cita mostrado en la tabla.
