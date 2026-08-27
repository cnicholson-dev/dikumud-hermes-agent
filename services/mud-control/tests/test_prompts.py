"""PTY-04: real prompts settle, prompt-like prose does not."""
from conftest import load

from mud_control import sanitize
from mud_control.prompts import PromptKind, classify, expects_secret, is_settled


def cleaned_text(name: str) -> str:
    return sanitize.clean(load(name)).game_text


def test_login_name_prompt_is_recognised():
    assert classify(cleaned_text("01-connect-and-telnet-negotiation.bin")) is PromptKind.NAME


def test_game_prompt_after_a_command():
    assert classify(cleaned_text("04-normal-command-and-prompt.bin")) is PromptKind.GAME


def test_creation_prompts_are_distinguished():
    text = cleaned_text("03-first-use-creation-prompts.bin")
    # The capture deliberately stops at the sex prompt, which is the agent's
    # decision and not the bootstrap's.
    assert classify(text) is PromptKind.SEX


def test_prompt_like_prose_does_not_settle():
    # Real game text containing '?' and '>' mid-stream must not look settled.
    prose = (
        "You say 'Is this a prompt? >'\n"
        "The Cityguard looks at you.\n"
    )
    assert classify(prose) is PromptKind.NONE
    assert not is_settled(prose)


def test_trailing_newline_never_settles():
    # The server is still talking; a newline means more is coming.
    assert classify("Some output\n> \n") is PromptKind.NONE


def test_prompt_must_be_at_the_end():
    # A prompt earlier in the buffer, with output after it, is stale.
    assert classify("> \nYou are hungry.\n") is PromptKind.NONE


def test_name_reprompt_on_one_line_is_recognised():
    # DikuMUD sends this without a line break before 'Name:'.
    assert classify("Illegal name, please try another.Name: ") is PromptKind.NAME


def test_password_prompts_are_marked_secret():
    for kind in (PromptKind.PASSWORD, PromptKind.PASSWORD_NEW,
                 PromptKind.PASSWORD_CONFIRM):
        assert expects_secret(kind)
    for kind in (PromptKind.GAME, PromptKind.NAME, PromptKind.MENU,
                 PromptKind.SEX, PromptKind.CLASS):
        assert not expects_secret(kind)


def test_empty_buffer_is_not_a_prompt():
    assert classify("") is PromptKind.NONE
