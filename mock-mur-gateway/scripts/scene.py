"""Scene trigger test scripts."""

import asyncio
from . import test_script


@test_script("scene-discrete", "Cycle through scene names via discrete trigger value")
async def scene_discrete(ctx):
    """Sends trigger events where the value is a scene name.

    Usage:
        --trigger SceneSelector --extra scenes=day,night,default
        --trigger SceneSelector --extra scenes=day,night,bogus  (tests fallback)
    """
    scenes = ctx.extra.get("scenes", "default").split(",")
    ctx.log(f"Scene list: {scenes}")
    while True:
        for scene in scenes:
            ctx.cycle += 1
            ctx.log(f"--- cycle {ctx.cycle}: switching to '{scene}' ---")
            await ctx.send(ctx.trigger, scene)
            await ctx.wait(ctx.on_time)


@test_script("scene-buttons", "Simulate button triggers for scene switching")
async def scene_buttons(ctx):
    """Sends On/Off for button triggers mapped to scenes.

    Usage:
        --extra buttons=Button.Day:day,Button.Night:night
    The --trigger flag is ignored; each button mapping specifies its own trigger name.
    """
    mapping_str = ctx.extra.get("buttons", "Button.Day:day,Button.Night:night")
    mappings = []
    for pair in mapping_str.split(","):
        parts = pair.strip().split(":")
        if len(parts) == 2:
            mappings.append((parts[0].strip(), parts[1].strip()))

    ctx.log(f"Button mappings: {mappings}")
    while True:
        for trigger_name, scene_name in mappings:
            ctx.cycle += 1
            ctx.log(f"--- cycle {ctx.cycle}: '{trigger_name}' -> scene '{scene_name}' ---")
            await ctx.send(trigger_name, "On")
            await ctx.wait(0.5)
            await ctx.send(trigger_name, "Off")
            await ctx.wait(ctx.on_time)
