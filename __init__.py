"""Loshify 1.0: a Spotify curator plugin for Hermes.

``register(ctx)`` adds seven curator tools, the ``pre_llm_call`` taste brief and the bundled
``loshify-curator`` skill. Nothing here imports Hermes at module load: ``hermes plugins validate``
probes ``register`` in a bare interpreter, and the real store/transport are only touched when a tool
runs.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN_NAME = "loshify"
__version__ = "1.0.0"

# Must equal plugin.yaml provides_tools / provides_hooks (tests/test_manifest.py).
TOOLS: tuple[str, ...] = ("loshify_setup", "loshify_taste", "loshify_reactions", "loshify_curate", "loshify_react",
                         "loshify_rotation", "loshify_onboard")
HOOKS: tuple[str, ...] = ("pre_llm_call",)
TOOLSET = "loshify"
SKILL_NAME = "loshify-curator"
SKILL_PATH = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"

_EMOJI = {"loshify_setup": "🎧", "loshify_taste": "🧭", "loshify_reactions": "💬", "loshify_curate": "🎛️",
          "loshify_react": "❤️", "loshify_rotation": "🔁", "loshify_onboard": "👋"}


def _runtime(ctx):
    from .tools.handlers import Runtime

    def get_setting(key, default):
        getter = getattr(ctx, "get_config", None)
        return getter(key, default) if callable(getter) else default

    return Runtime(get_setting=get_setting)


def register(ctx) -> None:
    """Hermes entry point."""
    from .tools import handlers, schemas

    runtime = _runtime(ctx)
    by_name = {schema["name"]: schema for schema in schemas.ALL}
    for name in TOOLS:
        impl = handlers.TOOL_HANDLERS[name]

        def handler(args, _impl=impl, **_kwargs):
            return _impl(runtime, args)

        schema = by_name[name]
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=_EMOJI[name])

    def pre_llm_call(user_message="", **kwargs):
        return handlers.taste_hook(runtime, user_message, **kwargs)

    ctx.register_hook("pre_llm_call", pre_llm_call)

    register_skill = getattr(ctx, "register_skill", None)
    if callable(register_skill) and SKILL_PATH.is_file():
        register_skill(SKILL_NAME, SKILL_PATH,
                       description="Loshify curator loop: setup, portrait, rotations, Time Capsule, Deep Dig, reactions.")
