import os
import json
import uuid
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

SHEET_NAME = "Appointments"
HEADERS = [
    "id",
    "name",
    "email",
    "phone",
    "start_time_iso",
    "local_display",
    "status",
    "calendar_event_id",
    "created_at_iso",
    "notes",
]
BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 18
DEFAULT_DURATION_MINUTES = 30
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_timezone() -> ZoneInfo:
    tz_name = os.getenv("TZ", "America/Bogota")
    return ZoneInfo(tz_name)


tz = get_timezone()


def now_local() -> datetime:
    return datetime.now(tz)


def combine_datetime(selected_date: date, selected_time: time) -> datetime:
    return datetime.combine(selected_date, selected_time, tzinfo=tz)


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt_val = datetime.fromisoformat(value)
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=tz)
        return dt_val.astimezone(tz)
    except ValueError:
        return None


def generate_slots_for_date(selected_date: date) -> List[datetime]:
    start_dt = datetime.combine(
        selected_date, time(hour=BUSINESS_START_HOUR, minute=0), tzinfo=tz
    )
    end_dt = datetime.combine(
        selected_date, time(hour=BUSINESS_END_HOUR, minute=0), tzinfo=tz
    )
    slots: List[datetime] = []
    current = start_dt
    while current < end_dt:
        slots.append(current)
        current += timedelta(minutes=30)
    return slots


def build_conflict_set(
    existing: List[Dict], selected_date: date, ignore_id: Optional[str] = None
) -> set:
    conflicts = set()
    for item in existing:
        if item.get("status") != "active":
            continue
        if ignore_id and item.get("id") == ignore_id:
            continue
        raw_iso = item.get("start_time_iso", "")
        dt_val = parse_iso_datetime(raw_iso)
        item_date = dt_val.date() if dt_val else None
        # Fallback: compare by date prefix if parse fails
        if item_date is None and len(raw_iso) >= 10:
            try:
                item_date = datetime.fromisoformat(raw_iso[:10]).date()
            except ValueError:
                item_date = None

        if item_date and item_date == selected_date:
            stamp = (
                dt_val.replace(second=0, microsecond=0).isoformat()
                if dt_val
                else f"{selected_date.isoformat()}T{raw_iso[11:16]}"
            )
            conflicts.add(stamp)
    return conflicts


def slot_choices(
    existing: List[Dict], selected_date: date, ignore_id: Optional[str] = None
) -> List[Dict]:
    slots = generate_slots_for_date(selected_date)
    conflicts = build_conflict_set(existing, selected_date, ignore_id)
    data: List[Dict] = []
    for slot in slots:
        iso_slot = slot.replace(second=0, microsecond=0).isoformat()
        is_busy = iso_slot in conflicts
        label = (
            f"🔴 {slot.strftime('%I:%M %p')} (ocupada)"
            if is_busy
            else f"🟢 {slot.strftime('%I:%M %p')}"
        )
        data.append(
            {"dt": slot, "status": "busy" if is_busy else "free", "label": label}
        )
    return data


def is_within_business_hours(dt_value: datetime) -> bool:
    start_ok = dt_value.hour >= BUSINESS_START_HOUR
    end_ok = dt_value.hour < BUSINESS_END_HOUR or (
        dt_value.hour == BUSINESS_END_HOUR and dt_value.minute == 0
    )
    return start_ok and end_ok


def load_user_credentials() -> Credentials:
    """Carga credenciales OAuth de usuario. Usa archivo de cliente y guarda token renovable."""

    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE") or os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE"
    )
    raw_json = os.getenv("GOOGLE_OAUTH_CLIENT_JSON") or os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    token_path = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", ".streamlit/oauth_token.json")

    if not client_file and not raw_json:
        raise ValueError(
            "Configura GOOGLE_OAUTH_CLIENT_FILE (o JSON) para Calendar y Sheets"
        )

    client_config: Optional[Dict] = None
    if raw_json:
        try:
            client_config = json.loads(raw_json)
        except json.JSONDecodeError as exc:  # noqa: BLE001
            raise ValueError("GOOGLE_OAUTH_CLIENT_JSON no es JSON válido") from exc

    if not client_config and (not client_file or not os.path.exists(client_file)):
        raise ValueError("No se encontró archivo de cliente OAuth")

    creds: Optional[Credentials] = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = (
            InstalledAppFlow.from_client_config(client_config, SCOPES)
            if client_config
            else InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
        )
        creds = flow.run_local_server(port=0)

        token_dir = os.path.dirname(token_path)
        if token_dir:
            os.makedirs(token_dir, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return creds


@st.cache_resource(show_spinner=False)
def get_google_services():
    creds = load_user_credentials()
    calendar = build("calendar", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return calendar, sheets


def ensure_sheet_headers(sheets_service) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise ValueError("Falta GOOGLE_SHEETS_SPREADSHEET_ID")

    try:
        current = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_NAME}!1:1")
            .execute()
        )
        values = current.get("values", [])
        if not values or values[0] != HEADERS:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{SHEET_NAME}!1:1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
    except HttpError as exc:
        if exc.resp.status == 400:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": SHEET_NAME,
                                }
                            }
                        }
                    ]
                },
            ).execute()
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{SHEET_NAME}!1:1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
        else:
            raise


def fetch_appointments(sheets_service) -> List[Dict]:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        return []

    ensure_sheet_headers(sheets_service)
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_NAME}!A2:J")
        .execute()
    )
    rows = result.get("values", [])
    data: List[Dict] = []
    for row in rows:
        item = {
            key: (row[idx] if idx < len(row) else "") for idx, key in enumerate(HEADERS)
        }
        data.append(item)
    return data


def append_appointment(sheets_service, values: List[str]) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise ValueError("Falta GOOGLE_SHEETS_SPREADSHEET_ID")

    ensure_sheet_headers(sheets_service)
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()


def update_row(sheets_service, row_number: int, values: List[str]) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise ValueError("Falta GOOGLE_SHEETS_SPREADSHEET_ID")

    range_ref = f"{SHEET_NAME}!A{row_number}:J{row_number}"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_ref,
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()


def send_email(to_email: str, subject: str, body: str) -> None:
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not password:
        st.warning(
            "No se configuró GMAIL_USER o GMAIL_APP_PASSWORD. Correo no enviado."
        )
        return
    if not to_email:
        st.warning("No se proporcionó correo destino. Correo no enviado.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, context=context, timeout=30
        ) as server:
            server.login(user, password)
            server.send_message(msg)
    except ssl.SSLEOFError:
        # Fallback a STARTTLS si el túnel SSL directo falla (EOF).
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Error al enviar correo: {exc}")


def create_calendar_event(
    calendar_service, summary: str, start_dt: datetime, duration_minutes: int
) -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    event_body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(tz)},
        "end": {
            "dateTime": (start_dt + timedelta(minutes=duration_minutes)).isoformat(),
            "timeZone": str(tz),
        },
    }
    event = (
        calendar_service.events()
        .insert(calendarId=calendar_id, body=event_body, sendUpdates="none")
        .execute()
    )
    return event.get("id", "")


def update_calendar_event(
    calendar_service, event_id: str, start_dt: datetime, duration_minutes: int
) -> None:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    body = {
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(tz)},
        "end": {
            "dateTime": (start_dt + timedelta(minutes=duration_minutes)).isoformat(),
            "timeZone": str(tz),
        },
    }
    calendar_service.events().patch(
        calendarId=calendar_id, eventId=event_id, body=body, sendUpdates="none"
    ).execute()


def delete_calendar_event(calendar_service, event_id: str) -> None:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    calendar_service.events().delete(
        calendarId=calendar_id, eventId=event_id, sendUpdates="none"
    ).execute()


def format_local(dt_value: datetime) -> str:
    return dt_value.strftime("%Y-%m-%d %I:%M %p (%Z)")


def has_conflict(
    existing: List[Dict], target_iso: str, ignore_id: Optional[str] = None
) -> bool:
    for item in existing:
        if item.get("status") != "active":
            continue
        if ignore_id and item.get("id") == ignore_id:
            continue
        if item.get("start_time_iso") == target_iso:
            return True
    return False


def find_by_id(
    existing: List[Dict], appointment_id: str
) -> Tuple[Optional[Dict], Optional[int]]:
    for idx, item in enumerate(existing):
        if item.get("id") == appointment_id:
            return item, idx
    return None, None


def filter_by_email(existing: List[Dict], email: str) -> List[Dict]:
    return [item for item in existing if item.get("email", "").lower() == email.lower()]


def render_header():
    st.title("Agendamiento de Citas")
    st.caption("Horarios: 8:00 am a 6:00 pm (UTC-5)")
    st.info("No se permiten citas en el pasado y no se duplican horarios.")


def render_sidebar_status():
    try:
        calendar_service, sheets_service = get_google_services()
        if calendar_service and sheets_service:
            st.sidebar.success("Conectado a Google APIs")
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"Google APIs no configuradas: {exc}")


def handle_booking(existing: List[Dict]) -> None:
    with st.form("book_form"):
        name = st.text_input("Nombre", max_chars=80)
        email = st.text_input("Email")
        phone = st.text_input("Teléfono", max_chars=30)
        selected_date = st.date_input("Fecha", min_value=date.today())
        slots_info = slot_choices(existing, selected_date)
        selected_slot_info = st.selectbox(
            "Hora",
            options=slots_info,
            format_func=lambda item: item["label"],
            key=f"slot_booking_{selected_date.isoformat()}",
            help="Slots de 30 minutos entre 8am y 6pm (🟢 disponibles / 🔴 ocupados)",
        )
        if selected_slot_info and selected_slot_info["status"] == "free":
            selected_slot = selected_slot_info["dt"]
        else:
            selected_slot = None
            st.warning("Selecciona un horario disponible (verde).")
        reason = st.text_area("Motivo / notas", max_chars=300)
        submitted = st.form_submit_button("Agendar cita")

    if not submitted:
        return

    if not selected_slot:
        st.error("No hay horarios disponibles para esta hora.")
        return

    start_dt = selected_slot
    if start_dt < now_local():
        st.error("No puedes agendar en el pasado.")
        return
    if not is_within_business_hours(start_dt):
        st.error("Fuera del horario de atención (8 am - 6 pm).")
        return

    start_iso = start_dt.isoformat()
    if has_conflict(existing, start_iso):
        st.error("Ya existe una cita en ese horario.")
        return

    try:
        calendar_service, sheets_service = get_google_services()
        appointment_id = str(uuid.uuid4())
        summary = f"Cita con {name}" if name else "Cita"
        event_id = create_calendar_event(
            calendar_service, summary, start_dt, DEFAULT_DURATION_MINUTES
        )

        created_at = now_local().isoformat()
        values = [
            appointment_id,
            name,
            email,
            phone,
            start_iso,
            format_local(start_dt),
            "active",
            event_id,
            created_at,
            reason,
        ]
        append_appointment(sheets_service, values)

        email_body = (
            f"Hola {name or ''}, tu cita está agendada el {format_local(start_dt)}. "
            f"ID: {appointment_id}\nMotivo: {reason}"
        )
        send_email(email, "Confirmación de cita", email_body)
        st.success(f"Cita agendada. ID: {appointment_id}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Error al agendar: {exc}")


def handle_lookup(existing: List[Dict]) -> List[Dict]:
    st.subheader("Mis citas")
    email = st.text_input("Email para consultar")
    if not email:
        return []
    user_rows = filter_by_email(existing, email)
    active = [row for row in user_rows if row.get("status") == "active"]
    if active:
        df = pd.DataFrame(active)
        st.dataframe(df[["id", "local_display", "status", "notes"]])
    else:
        st.info("No hay citas activas para este email.")
    return active


def handle_update(existing: List[Dict], user_rows: List[Dict]) -> None:
    st.subheader("Actualizar cita")
    if not user_rows:
        st.caption("Ingresa un email arriba para ver tus citas.")
        return

    ids = [row["id"] for row in user_rows]
    selected_id = st.selectbox("Selecciona la cita", ids) if ids else None
    if not selected_id:
        return

    selected_date = st.date_input(
        "Nueva fecha", min_value=date.today(), key="update_date"
    )

    slots_info = slot_choices(existing, selected_date, ignore_id=selected_id)
    selected_slot_info = st.selectbox(
        "Nueva hora",
        options=slots_info,
        format_func=lambda item: item["label"],
        key=f"update_time_{selected_id}_{selected_date.isoformat()}",
        help="Slots de 30 minutos entre 8am y 6pm (🟢 disponibles / 🔴 ocupados)",
    )
    if selected_slot_info and selected_slot_info["status"] == "free":
        selected_slot = selected_slot_info["dt"]
    else:
        selected_slot = None
        st.warning("Selecciona un horario disponible (verde).")
    notes = st.text_area("Notas adicionales", key="update_notes")
    if st.button("Actualizar"):
        if not selected_slot:
            st.error("No hay horarios disponibles para esta hora.")
            return

        start_dt = selected_slot
        if start_dt < now_local():
            st.error("No puedes agendar en el pasado.")
            return
        if not is_within_business_hours(start_dt):
            st.error("Fuera del horario de atención (8 am - 6 pm).")
            return

        start_iso = start_dt.isoformat()
        if has_conflict(existing, start_iso, ignore_id=selected_id):
            st.error("Ya existe una cita en ese horario.")
            return

        target, idx = find_by_id(existing, selected_id)
        if not target or idx is None:
            st.error("No se encontró la cita.")
            return

        try:
            calendar_service, sheets_service = get_google_services()
            event_id = target.get("calendar_event_id", "")
            if event_id:
                update_calendar_event(
                    calendar_service, event_id, start_dt, DEFAULT_DURATION_MINUTES
                )

            updated_row = [
                target.get("id", ""),
                target.get("name", ""),
                target.get("email", ""),
                target.get("phone", ""),
                start_iso,
                format_local(start_dt),
                "active",
                event_id,
                target.get("created_at_iso", target.get("created_at", "")),
                notes or target.get("notes", ""),
            ]
            update_row(sheets_service, idx + 2, updated_row)

            email_body = (
                f"Tu cita {selected_id} fue reprogramada a {format_local(start_dt)}."
            )
            send_email(target.get("email", ""), "Cita reprogramada", email_body)
            st.success("Cita actualizada.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error al actualizar: {exc}")


def handle_cancel(existing: List[Dict], user_rows: List[Dict]) -> None:
    st.subheader("Cancelar cita")
    if not user_rows:
        st.caption("Ingresa un email arriba para ver tus citas.")
        return

    ids = [row["id"] for row in user_rows]
    selected_id = st.selectbox("Selecciona la cita a cancelar", ids, key="cancel_id")
    reason = st.text_input("Motivo de cancelación")
    if st.button("Cancelar cita"):
        target, idx = find_by_id(existing, selected_id)
        if not target or idx is None:
            st.error("No se encontró la cita.")
            return

        try:
            calendar_service, sheets_service = get_google_services()
            event_id = target.get("calendar_event_id", "")
            if event_id:
                delete_calendar_event(calendar_service, event_id)

            canceled_row = [
                target.get("id", ""),
                target.get("name", ""),
                target.get("email", ""),
                target.get("phone", ""),
                target.get("start_time_iso", ""),
                target.get("local_display", ""),
                "canceled",
                event_id,
                target.get("created_at_iso", target.get("created_at", "")),
                reason,
            ]
            update_row(sheets_service, idx + 2, canceled_row)

            email_body = f"Tu cita {selected_id} fue cancelada. Motivo: {reason}"
            send_email(target.get("email", ""), "Cita cancelada", email_body)
            st.success("Cita cancelada.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error al cancelar: {exc}")


def main():
    render_header()
    render_sidebar_status()

    try:
        _, sheets_service = get_google_services()
        appointments = fetch_appointments(sheets_service)
    except Exception:
        appointments = []
        st.warning("Configura Google APIs para habilitar agenda persistente.")

    tabs = st.tabs(["Agendar", "Mis citas"])

    with tabs[0]:
        handle_booking(appointments)

    with tabs[1]:
        user_rows = handle_lookup(appointments)
        handle_update(appointments, user_rows)
        handle_cancel(appointments, user_rows)


if __name__ == "__main__":
    st.set_page_config(page_title="Agendamiento", page_icon="🗓️", layout="wide")
    main()
