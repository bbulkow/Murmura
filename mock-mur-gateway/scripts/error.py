"""Error-condition trigger test scripts."""

import random
from . import test_script


@test_script("missing-off", "Sends On but occasionally skips the Off (tests missing key-up)")
async def missing_off(ctx):
    """Every ~4th cycle, the Off event is skipped. Device must handle orphaned On."""
    while True:
        ctx.cycle += 1
        ctx.log(f"--- cycle {ctx.cycle} (elapsed {ctx.elapsed:.0f}s) ---")
        await ctx.send(ctx.trigger, "On")
        await ctx.wait(ctx.on_time)

        if random.random() < 0.25:
            ctx.log("  ** SKIPPING Off (simulating lost key-up) **")
        else:
            await ctx.send(ctx.trigger, "Off")

        await ctx.wait(ctx.off_time)


@test_script("missing-on", "Occasionally skips the On, sends only Off (tests orphan key-up)")
async def missing_on(ctx):
    """Every ~4th cycle, the On event is skipped. Device receives Off without preceding On."""
    while True:
        ctx.cycle += 1
        ctx.log(f"--- cycle {ctx.cycle} (elapsed {ctx.elapsed:.0f}s) ---")

        if random.random() < 0.25:
            ctx.log("  ** SKIPPING On (simulating lost key-down) **")
        else:
            await ctx.send(ctx.trigger, "On")

        await ctx.wait(ctx.on_time)
        await ctx.send(ctx.trigger, "Off")
        await ctx.wait(ctx.off_time)
