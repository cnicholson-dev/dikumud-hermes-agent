"""The spectator feed: what it writes, and what it must never grow into.

The play log is the one file in this service that holds content rather than
metadata, so the two properties worth testing are that it records what the game
said faithfully and that it cannot grow without bound. Its third property, that
no credential reaches it, is tested in `test_credential_leak.py` beside the
other disclosure tests rather than here.
"""
import os

from mud_control.playlog import PlayLog


def test_game_text_is_appended_verbatim(tmp_path):
    log = PlayLog(tmp_path / "play.log")
    log.write("The Temple Of Midgaard\n")
    log.write("You see a cityguard here.\n")
    log.close()

    assert (tmp_path / "play.log").read_text() == (
        "The Temple Of Midgaard\n"
        "You see a cityguard here.\n")


def test_a_command_lands_after_the_prompt_the_game_wrote(tmp_path):
    # DikuMUD's prompt is "> " with no trailing newline, so a bare command
    # completes the line and the feed reads the way the session looked. An
    # earlier version wrote its own "> " marker and produced "> > north".
    log = PlayLog(tmp_path / "play.log")
    log.write("> ")
    log.command("north")
    log.write("The Temple Altar\n")
    log.close()

    assert (tmp_path / "play.log").read_text() == (
        "> north\n"
        "The Temple Altar\n")


def test_empty_writes_are_ignored(tmp_path):
    log = PlayLog(tmp_path / "play.log")
    log.write("")
    log.command("")
    log.close()

    assert (tmp_path / "play.log").read_text() == ""


def test_the_file_is_not_world_readable(tmp_path):
    log = PlayLog(tmp_path / "play.log")
    log.write("x\n")
    log.close()

    mode = os.stat(tmp_path / "play.log").st_mode & 0o777
    assert mode == 0o640, "same mode as the audit record"


def test_it_rotates_once_at_the_cap(tmp_path):
    # Game text arrives at the speed the game talks, unlike the audit record's
    # one line per command, so an unbounded feed would eventually fill the
    # volume that also holds the evidence.
    log = PlayLog(tmp_path / "play.log", max_bytes=64)
    log.write("a" * 40 + "\n")
    log.write("b" * 40 + "\n")   # crosses the cap, rotates after writing
    log.write("c" * 10 + "\n")
    log.close()

    live = tmp_path / "play.log"
    previous = tmp_path / "play.log.1"
    assert previous.exists(), "the crossed generation is kept, not discarded"
    assert previous.read_text().startswith("a" * 40)
    assert live.read_text() == "c" * 10 + "\n", "the live file starts fresh"


def test_rotation_keeps_one_generation(tmp_path):
    log = PlayLog(tmp_path / "play.log", max_bytes=16)
    for round_ in range(4):
        log.write(f"{round_}" * 20 + "\n")
    log.close()

    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == ["play.log", "play.log.1"], "one generation, not a pile"


def test_an_existing_file_is_appended_to_not_truncated(tmp_path):
    (tmp_path / "play.log").write_text("from an earlier session\n")

    log = PlayLog(tmp_path / "play.log")
    log.write("and this one\n")
    log.close()

    assert (tmp_path / "play.log").read_text() == (
        "from an earlier session\n"
        "and this one\n")
