"""Basic trigger test scripts."""

import asyncio
from . import test_script


@test_script("basic-on-off", "Simple On/Off cycle with configurable timing")
async def basic_on_off(ctx):
    """Sends On, waits on_time, sends Off, waits off_time, repeats forever."""
    while True:
        ctx.cycle += 1
        ctx.log(f"--- cycle {ctx.cycle} (elapsed {ctx.elapsed:.0f}s) ---")
        await ctx.send(ctx.trigger, "On")
        await ctx.wait(ctx.on_time)
        await ctx.send(ctx.trigger, "Off")
        await ctx.wait(ctx.off_time)


@test_script("rapid-fire", "Fast On/Off cycles (0.5s on, 0.5s off) to stress-test timing")
async def rapid_fire(ctx):
    """Rapid On/Off with short intervals. Uses on_time/off_time if set, otherwise 0.5s each."""
    on_t = ctx.on_time if ctx.on_time != 5.0 else 0.5
    off_t = ctx.off_time if ctx.off_time != 5.0 else 0.5
    while True:
        ctx.cycle += 1
        if ctx.cycle % 50 == 1:
            ctx.log(f"--- cycle {ctx.cycle} (elapsed {ctx.elapsed:.0f}s) ---")
        await ctx.send(ctx.trigger, "On")
        await ctx.wait(on_t)
        await ctx.send(ctx.trigger, "Off")
        await ctx.wait(off_t)
