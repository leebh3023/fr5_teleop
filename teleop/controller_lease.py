from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ControllerLease:
    session_id: str | None = None
    last_seq: int = -1

    def claim(self, session_id: str) -> bool:
        if self.session_id not in {None, session_id}:
            return False
        if self.session_id != session_id:
            self.last_seq = -1
        self.session_id = session_id
        return True

    def release(self, session_id: str) -> bool:
        if self.session_id != session_id:
            return False
        self.session_id = None
        self.last_seq = -1
        return True

    def owns(self, session_id: str) -> bool:
        return self.session_id == session_id

    def accept_sequence(self, session_id: str, seq: int) -> bool:
        if not self.owns(session_id) or seq <= self.last_seq:
            return False
        self.last_seq = seq
        return True
