from datetime import datetime
from typing import Optional


class AgentLogger:
    def __init__(self):
        self.logs: list[dict] = []
        self._step = 0

    def log(
        self,
        action: str,
        tool: Optional[str] = None,
        input_data: Optional[dict] = None,
        output_preview: Optional[str] = None,
        status: str = "ok",
    ) -> dict:
        self._step += 1
        entry = {
            "step": self._step,
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "tool": tool,
            "input": input_data,
            "output_preview": output_preview[:200] if output_preview else None,
            "status": status,
        }
        self.logs.append(entry)
        print(f"[AGENT STEP {self._step}] {action}" + (f" → {tool}" if tool else ""))
        return entry

    def get_logs(self) -> list[dict]:
        return self.logs
