"""Shared test helpers."""


def fake_daemon(player):
    """Make a client think it is connected, record the commands it sends, and apply the obvious ones."""
    sent = []

    def send(name, **args):
        sent.append((name, args))
        if name == "play_index":
            player.queue.current_idx = args["index"]
        return True

    player._send = send
    player._connected = True
    return sent
