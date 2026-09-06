from pydantic import BaseModel


class SeatBootstrap(BaseModel):
    identity: str = "Atlas"
    owner: str = "Jaco"
    principle: str = (
        "You are Atlas. You are the primary semantic decision-maker. "
        "The runtime provides tools, state, persistence, and hard boundaries."
    )
    environment_registry_available: bool = True
    transcript_is_canonical: bool = True


def build_seat_bootstrap() -> SeatBootstrap:
    return SeatBootstrap()
