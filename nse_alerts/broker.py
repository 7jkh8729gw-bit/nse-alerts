"""Picks the data broker from BROKER=upstox|angel. Everything else is broker-agnostic."""
from . import angel, config, upstox


def load_token_map(force: bool = False) -> dict:
    return (upstox if config.BROKER == "upstox" else angel).load_token_map(force)


def make_client():
    return upstox.UpstoxClient() if config.BROKER == "upstox" else angel.AngelClient()


def make_stream(client, tokens, on_tick):
    cls = upstox.Stream if config.BROKER == "upstox" else angel.Stream
    return cls(client, tokens, on_tick)
