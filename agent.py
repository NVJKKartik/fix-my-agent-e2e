"""Front-desk chat agent for a small clinic.

Serves an OpenAI-compatible chat endpoint (POST /v1/chat/completions, GET /health) using only
the standard library. Behaviour is set by instructions.md; appointments live in memory.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTRUCTIONS_FILE = Path(__file__).with_name("instructions.md")
MAX_TOOL_ROUNDS = 6
SLOT_MINUTES = 30

# weekday() -> (opens, closes); Sunday is closed.
HOURS = {
    0: ("08:00", "18:00"),
    1: ("08:00", "18:00"),
    2: ("08:00", "18:00"),
    3: ("08:00", "18:00"),
    4: ("08:00", "18:00"),
    5: ("09:00", "13:00"),
}
HOURS_TEXT = "Monday to Friday 08:00-18:00, Saturday 09:00-13:00, closed on Sunday."

_appointments: dict[str, dict[str, str]] = {}
_next_id = 1


def _next_weekday(start: date, weekday: int) -> date:
    return start + timedelta(days=(weekday - start.weekday()) % 7 or 7)


def _seed() -> None:
    today = date.today()
    _add("Priya Sharma", "5550101", _next_weekday(today, 1), "10:00", "check-up")
    _add("Daniel Okoye", "5550102", _next_weekday(today, 2), "14:30", "dental cleaning")


def _add(name: str, phone: str, day: date, time: str, reason: str) -> dict[str, str]:
    global _next_id
    appointment = {
        "id": f"A{_next_id:03d}",
        "patient_name": name,
        "phone": phone,
        "date": day.isoformat(),
        "time": time,
        "reason": reason,
    }
    _next_id += 1
    _appointments[appointment["id"]] = appointment
    return appointment


def _check_slot(day_text: str, time_text: str, ignore_id: str = "") -> str | None:
    """Return a reason the slot cannot be used, or None when it is free."""
    try:
        day = date.fromisoformat(day_text)
        slot = datetime.strptime(time_text, "%H:%M")
    except ValueError:
        return "date must be YYYY-MM-DD and time must be HH:MM (24h)"
    if day < date.today():
        return "that date is in the past"
    window = HOURS.get(day.weekday())
    if window is None:
        return "the clinic is closed that day"
    if not window[0] <= time_text < window[1]:
        return f"the clinic is open {window[0]}-{window[1]} that day"
    if slot.minute % SLOT_MINUTES:
        return "appointments start on the hour or half hour"
    for other in _appointments.values():
        if other["id"] != ignore_id and other["date"] == day_text and other["time"] == time_text:
            return "that slot is already taken"
    return None


def _owned(appointment_id: str, phone: str) -> dict[str, str] | None:
    found = _appointments.get(appointment_id)
    return found if found and found["phone"] == phone else None


def get_opening_hours() -> dict:
    return {"hours": HOURS_TEXT}


def find_appointments(patient_name: str, phone: str) -> dict:
    matches = [
        a
        for a in _appointments.values()
        if a["patient_name"].lower() == patient_name.strip().lower() and a["phone"] == phone
    ]
    return {"appointments": matches}


def book_appointment(patient_name: str, phone: str, date: str, time: str, reason: str) -> dict:
    problem = _check_slot(date, time)
    if problem:
        return {"ok": False, "error": problem}
    return {"ok": True, "appointment": _add(patient_name, phone, _parse(date), time, reason)}


def reschedule_appointment(appointment_id: str, phone: str, new_date: str, new_time: str) -> dict:
    found = _owned(appointment_id, phone)
    if found is None:
        return {"ok": False, "error": "no such appointment for this phone number"}
    problem = _check_slot(new_date, new_time, ignore_id=appointment_id)
    if problem:
        return {"ok": False, "error": problem}
    found["date"], found["time"] = new_date, new_time
    return {"ok": True, "appointment": found}


def cancel_appointment(appointment_id: str, phone: str) -> dict:
    found = _owned(appointment_id, phone)
    if found is None:
        return {"ok": False, "error": "no such appointment for this phone number"}
    del _appointments[appointment_id]
    return {"ok": True, "cancelled": found}


def _parse(day_text: str) -> date:
    return date.fromisoformat(day_text)


def _tool(name: str, description: str, properties: dict[str, str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {key: {"type": "string", "description": d} for key, d in properties.items()},
                "required": list(properties),
            },
        },
    }


TOOLS = [
    _tool("get_opening_hours", "Return the clinic opening hours.", {}),
    _tool(
        "find_appointments",
        "List a patient's upcoming appointments.",
        {"patient_name": "Full name", "phone": "The caller's phone number, as text (for example 555-0142)"},
    ),
    _tool(
        "book_appointment",
        "Book a new appointment. Date is YYYY-MM-DD, time is HH:MM (24h).",
        {
            "patient_name": "Full name",
            "phone": "The caller's phone number, as text (for example 555-0142)",
            "date": "YYYY-MM-DD",
            "time": "HH:MM",
            "reason": "Short reason for the visit",
        },
    ),
    _tool(
        "reschedule_appointment",
        "Move an existing appointment to a new date and time.",
        {
            "appointment_id": "Id from find_appointments",
            "phone": "The caller's phone number, as text (for example 555-0142)",
            "new_date": "YYYY-MM-DD",
            "new_time": "HH:MM",
        },
    ),
    _tool(
        "cancel_appointment",
        "Cancel an existing appointment.",
        {"appointment_id": "Id from find_appointments", "phone": "The caller's phone number, as text (for example 555-0142)"},
    ),
]
HANDLERS = {
    "get_opening_hours": get_opening_hours,
    "find_appointments": find_appointments,
    "book_appointment": book_appointment,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment": cancel_appointment,
}


def system_prompt() -> str:
    text = INSTRUCTIONS_FILE.read_text(encoding="utf-8")
    today = date.today()
    return text.replace("{{today}}", f"{today.strftime('%A')} {today.isoformat()}").replace(
        "{{hours}}", HOURS_TEXT
    )


def _llm(messages: list[dict]) -> dict:
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(
            {"model": os.environ["LLM_MODEL"], "messages": messages, "tools": TOOLS}
        ).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {os.environ['LLM_API_KEY']}",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)["choices"][0]["message"]


def _run_tool(call: dict) -> str:
    try:
        arguments = json.loads(call["function"]["arguments"] or "{}")
        return json.dumps(HANDLERS[call["function"]["name"]](**arguments))
    except Exception as exc:  # the model sees the failure and can correct the call
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def reply(history: list[dict]) -> tuple[str, list[dict]]:
    """The assistant's answer, and the tools it ran to give it."""
    conversation = [{"role": "system", "content": system_prompt()}]
    conversation += [m for m in history if m.get("role") in ("user", "assistant")]
    completed: list[dict] = []
    for _ in range(MAX_TOOL_ROUNDS):
        message = _llm(conversation)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or "", completed
        conversation.append(message)
        for call in calls:
            output = _run_tool(call)
            conversation.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            result = json.loads(output)
            completed.append(
                {
                    "id": uuid.uuid4().hex,
                    "name": call["function"]["name"],
                    "arguments": json.loads(call["function"]["arguments"] or "{}"),
                    "result": result,
                    "error": result.get("ok") is False,
                }
            )
    return "Sorry, I could not finish that request. Please call the clinic again.", completed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": "not_found"})
            return
        try:
            size = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
            content, completed = reply(body.get("messages") or [])
        except Exception as exc:
            self._send(500, {"error": {"type": type(exc).__name__, "message": str(exc)}})
            return
        self._send(
            200,
            {
                "object": "chat.completion",
                "model": os.environ.get("LLM_MODEL", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                # The tools this reply ran, in order: id, name, arguments, result, error.
                "completed_tools": completed,
            },
        )


def main() -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set")
    system_prompt()  # fail at startup if instructions.md is missing
    _seed()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
