"""Small state machine for confirmed market-regime transitions."""

from typing import NamedTuple, Optional


class RegimeObservation(NamedTuple):
    """Result from observing one raw regime classifier reading."""

    active: str
    changed: bool
    previous: Optional[str]
    candidate: str
    pending: Optional[str]
    pending_count: int
    required_confirmations: int


class RegimeHysteresis:
    """Require consecutive classifier readings before changing regime.

    A single noisy ADX/EMA reading should not move capital between spot and
    futures.  This state machine promotes a raw candidate only after it appears
    for `confirmations` consecutive observations.  Observing the already-active
    regime clears any pending candidate.
    """

    def __init__(self, active: str = "sideways", confirmations: int = 3):
        self.active = str(active or "sideways").lower()
        self.confirmations = max(1, int(confirmations or 1))
        self.pending: Optional[str] = None
        self.pending_count = 0

    def reset(self, active: str):
        """Reset active regime and clear pending candidates."""
        self.active = str(active or "sideways").lower()
        self.pending = None
        self.pending_count = 0

    def observe(self, candidate: str) -> RegimeObservation:
        """Observe one raw candidate and return the confirmed active regime."""
        candidate = str(candidate or "sideways").lower()

        if candidate == self.active:
            self.pending = None
            self.pending_count = 0
            return RegimeObservation(
                active=self.active,
                changed=False,
                previous=None,
                candidate=candidate,
                pending=None,
                pending_count=0,
                required_confirmations=self.confirmations,
            )

        if self.confirmations <= 1:
            previous = self.active
            self.active = candidate
            self.pending = None
            self.pending_count = 0
            return RegimeObservation(
                active=self.active,
                changed=True,
                previous=previous,
                candidate=candidate,
                pending=None,
                pending_count=self.confirmations,
                required_confirmations=self.confirmations,
            )

        if candidate == self.pending:
            self.pending_count += 1
        else:
            self.pending = candidate
            self.pending_count = 1

        if self.pending_count >= self.confirmations:
            previous = self.active
            self.active = candidate
            self.pending = None
            self.pending_count = 0
            return RegimeObservation(
                active=self.active,
                changed=True,
                previous=previous,
                candidate=candidate,
                pending=None,
                pending_count=self.confirmations,
                required_confirmations=self.confirmations,
            )

        return RegimeObservation(
            active=self.active,
            changed=False,
            previous=None,
            candidate=candidate,
            pending=self.pending,
            pending_count=self.pending_count,
            required_confirmations=self.confirmations,
        )
