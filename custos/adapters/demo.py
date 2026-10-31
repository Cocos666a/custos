"""A synthetic adapter used by ``custos demo``.

The demo has to show the whole chain -- reading, threshold, interlock, handover,
gate with a retry and an alert -- without a real agent installed, without
touching anything outside its temporary directory, and without a script that
answers differently on two runs. This adapter is what makes that possible.

Three parts of it are deliberate:

* the **reading** comes from a JSON file the demo writes into its sandbox, so the
  demo exercises the same ``Reading`` path every other adapter uses rather than
  inventing objects in memory;
* ``rotate()`` behaves like a real assisted adapter: it reports that it cannot
  start a session itself and says what the user should do;
* ``run_prompt()`` returns a **scripted** sequence of replies. Real tooling is
  what makes the demo non-deterministic, so the script stands in for it. The
  script is written in terms of the question rather than of literal text --
  ``"wrong"``, ``"right"``, ``"invalid"`` -- because the demo's question bank is
  walked in sequence mode, and a script of literal answers would silently become
  the wrong script as soon as the bank or the mode changed. The adapter holds a
  copy of the bank, so it can look up what "right" means for the question it was
  just asked.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..core.usage import Reading
from ..core.util import read_json
from .base import DOC_VERIFIED, Adapter, PromptResult, RotateResult, SessionRef

DEMO_TOOL = "demo"

# Script directives.
REPLY_RIGHT = "right"
REPLY_WRONG = "wrong"
REPLY_INVALID = "invalid"


class DemoAdapter(Adapter):
    """Scripted adapter with no external dependencies at all."""

    name = DEMO_TOOL
    BUSY_DETECTION = True  # always answers False; the scripted session is idle
    VERIFICATION = DOC_VERIFIED
    VERIFICATION_NOTE = (
        "synthetic: used only by `custos demo` to exercise the pipeline "
        "without a real agent; it never reads or writes anything outside the "
        "demo's temporary directory"
    )

    def __init__(
        self,
        config: Any = None,
        *,
        session: str = "demo-session-0001",
        state_path: str = "",
        script: Optional[List[str]] = None,
        bank: Optional[List[Dict[str, Any]]] = None,
        rotate_detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(config)
        self.session_id = session
        self.state_path = state_path
        # The last directive repeats once the script runs out, so a demo with
        # more attempts than scripted replies keeps behaving predictably.
        self.script = list(script or [REPLY_WRONG])
        self.bank = list(bank or [])
        self._reply_index = 0
        self.rotate_detail = rotate_detail or {}
        self.prompts: List[str] = []
        self.rotations: List[Dict[str, Any]] = []

    # -- reading ---------------------------------------------------------
    def detect(self) -> bool:
        return True

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        return [SessionRef(
            session=self.session_id,
            path=self.state_path,
            updated=os.path.getmtime(self.state_path) if os.path.exists(self.state_path) else 0.0,
            tool=self.name,
        )][:limit]

    def read_usage(self, session: SessionRef) -> Reading:
        payload = read_json(self.state_path, default=None)
        if not isinstance(payload, dict):
            raise self._read_failed(
                "demo state file %s is missing or unreadable" % self.state_path,
                session.session)
        return Reading(
            tool=self.name,
            session=self.session_id,
            tokens=int(payload.get("tokens") or 0),
            window=payload.get("window"),
            source="external",
            ts=float(payload.get("ts") or 0.0),
            compressed=bool(payload.get("compressed", False)),
            detail={"demo": True},
        )

    def busy(self, session: Any) -> Optional[bool]:
        """The scripted session is always idle, so the demo reaches the gate.

        ``False`` rather than ``None`` on purpose: ``None`` means "cannot tell"
        and the pipeline would defer, which is correct behaviour but not what a
        demo of the rotation path should show.
        """
        return False

    # -- rotation --------------------------------------------------------
    def rotate(
        self,
        session: str,
        handover_path: Optional[str],
        dry_run: bool = False,
        config: Any = None,
    ) -> RotateResult:
        self.rotations.append({"session": session, "handover": handover_path,
                               "dry_run": dry_run})
        if dry_run:
            return RotateResult(mode="assisted", rotated=False,
                                detail={"would_rotate": session})
        return RotateResult(
            mode="assisted",
            rotated=False,
            detail={
                "note": "the demo adapter never starts a session; a real adapter "
                        "may, when the tool offers a way to",
                "handover": handover_path,
                "next": self.start_hint(handover_path),
            },
        )

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return ("open a new session with your agent and paste the contents of %s"
                    % handover_path)
        return "open a new session with your agent"

    # -- gate ------------------------------------------------------------
    def answer_for(self, prompt: str) -> Optional[Any]:
        """The expected answer for whichever bank question is inside ``prompt``."""
        best: Optional[Dict[str, Any]] = None
        for entry in self.bank:
            text = str(entry.get("prompt") or "")
            if text and text in prompt:
                if best is None or len(text) > len(str(best.get("prompt") or "")):
                    best = entry
        return None if best is None else best.get("answer")

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Return the next scripted reply, resolved against the question asked."""
        self.prompts.append(prompt)
        index = min(self._reply_index, len(self.script) - 1)
        self._reply_index += 1
        directive = str(self.script[index] or REPLY_WRONG).strip().lower()

        if directive == REPLY_INVALID:
            return PromptResult(text="", rc=1, timed_out=True,
                                stderr="scripted timeout")
        if directive == REPLY_RIGHT:
            expected = self.answer_for(prompt)
            if expected is None:
                return PromptResult(
                    text="", rc=1,
                    stderr="the script asked for a correct answer but the "
                           "prompt matched no bank question")
            return PromptResult(text="answer: %s" % expected, rc=0)
        # "wrong": a plausible number that is not the answer. A model that has
        # lost the thread does not go silent, it produces something confident and
        # incorrect -- which is exactly the case the gate is for.
        expected = self.answer_for(prompt)
        try:
            wrong = int(expected) + 7 if expected is not None else 999
        except (TypeError, ValueError):
            wrong = "not-the-answer"
        return PromptResult(text="answer: %s" % wrong, rc=0)

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        caps.update({
            "history": False,
            "rotate": False,
            "full_auto": False,
            "run_prompt": True,
            "note": "synthetic adapter for the built-in demo",
        })
        return caps

    def one_shot_command(self) -> List[str]:
        return ["demo", "--print"]


def write_demo_session(path: str, tokens: int, window: int, *, compressed: bool = False,
                       ts: Optional[float] = None) -> str:
    """Write the synthetic session state file the demo adapter reads."""
    import time

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({
            "tokens": int(tokens),
            "window": int(window),
            "compressed": bool(compressed),
            "ts": float(ts if ts is not None else time.time()),
        }, stream, indent=1)
        stream.write("\n")
    return path
