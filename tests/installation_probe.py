"""Verify actual AstrBot initialization with an empty default target list."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

root = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve()
destination = root / "data/plugins/astrbot_plugin_self_amusement"
destination.parent.mkdir(parents=True)
shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", "data", ".venv"))
os.environ["ASTRBOT_ROOT"] = str(root)
sys.path.insert(0, str(root))

from astrbot.core import LogBroker, db_helper
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle


async def main():
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", AsyncMock()):
        await core.initialize()
    plugin = next(star for star in core.star_context.get_all_stars() if star.name == "astrbot_plugin_self_amusement")
    assert plugin.star_cls is not None and plugin.activated
    assert plugin.config["targets"] == []
    assert plugin.config.schema["targets"]["_special"] == "umo"
    assert plugin.star_cls.worker is not None
    previous = plugin.star_cls
    assert previous.reflection_worker is not None and previous.save_worker is not None
    success, error = await core.plugin_manager.reload(plugin.name)
    assert success, error
    assert previous.worker.done() and previous.reflection_worker.done() and previous.save_worker.done()
    await core.stop()
    print(json.dumps({"loaded": True, "native_configuration": True, "default_targets": [], "hot_reload": success}))


asyncio.run(main())
