"""Stress and edge-case trigger test scripts."""

from . import test_script


@test_script("long-hold", "On for 30s then Off — tests oneshot with extended holds")
async def long_hold(ctx):
    """Holds On for a long duration (default 30s, or use --on-time), then Off.
    Useful for testing oneshot triggers where the hold exceeds the audio loop length."""
    hold_time = ctx.on_time if ctx.on_time != 5.0 else 30.0
    while True:
        ctx.cycle += 1
        ctx.log(f"--- cycle {ctx.cycle} (elapsed {ctx.elapsed:.0f}s) ---")
        await ctx.send(ctx.trigger, "On")
        ctx.log(f"  holding for {hold_time}s...")
        await ctx.wait(hold_time)
        await ctx.send(ctx.trigger, "Off")
        await ctx.wait(ctx.off_time)


@test_script("multi-trigger", "Cycles through multiple triggers for multi-device testing")
async def multi_trigger(ctx):
    """Sends On/Off to multiple trigger names in round-robin.
    Trigger names are derived from --trigger by appending .1, .2, .3.
    Example: --trigger "Test" generates Test.1, Test.2, Test.3"""
    base = ctx.trigger
    triggers = [f"{base}.1", f"{base}.2", f"{base}.3"]
    ctx.log(f"Trigger names: {triggers}")
    ctx.log("Devices should subscribe to one of these names.")

    idx = 0
    while True:
        ctx.cycle += 1
        trigger_name = triggers[idx % len(triggers)]
        ctx.log(f"--- cycle {ctx.cycle} trigger={trigger_name} (elapsed {ctx.elapsed:.0f}s) ---")

        await ctx.send(trigger_name, "On")
        await ctx.wait(ctx.on_time)
        await ctx.send(trigger_name, "Off")
        await ctx.wait(ctx.off_time)

        idx += 1
