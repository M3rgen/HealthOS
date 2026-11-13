from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.provenance import new_context_id, provenance
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.parsers.common import temporal
from app.schemas.status import Probe


class DiscoveryService:
    def __init__(
        self,
        client: GoogleHealthClient,
        specs: dict[str, DataTypeSpec],
        concurrency: int = 2,
    ):
        self.client = client
        self.specs = specs
        self.concurrency = concurrency

    async def probe(self, days: int = 30) -> list[Probe]:
        # Fail once and clearly before launching one failing task per data type.
        await self.client.tokens.get()
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        semaphore = asyncio.Semaphore(self.concurrency)
        run = new_context_id("probe")

        async def one(spec: DataTypeSpec) -> Probe:
            async with semaphore:
                try:
                    probe_start = (
                        max(start, end - timedelta(days=spec.max_window_days))
                        if spec.max_window_days
                        else start
                    )
                    with provenance(source="probe", context_id=run):
                        page = await self.client.list_data_points(
                            spec, probe_start, end, page_size=1
                        )
                    # No record is archived from a probe, but what Google sent is kept.
                    await self.client.keep(page.responses)
                    sample = page.data_points[0] if page.data_points else None
                    sample_start = sample_end = None
                    keys: list[str] = []
                    if isinstance(sample, dict):
                        sample_start, sample_end, _ = temporal(spec, sample)
                        keys = sorted(sample)
                    return {
                        "data_type": spec.name,
                        "access_method": spec.access_method,
                        "accessible": True,
                        "has_recent_data": sample is not None,
                        "sample_start": sample_start,
                        "sample_end": sample_end,
                        "top_level_fields": keys,
                    }
                except Exception:
                    return {
                        "data_type": spec.name,
                        "access_method": spec.access_method,
                        "accessible": False,
                        "has_recent_data": False,
                        "error_code": "discovery_probe_failed",
                        "error": "Veri türüne erişim denetlenemedi. Daha sonra yeniden deneyin.",
                    }

        return await asyncio.gather(*(one(spec) for spec in self.specs.values()))

    async def devices(self) -> list[dict[str, Any]]:
        with provenance(source="devices"):
            payload = await self.client.list_paired_devices()
        devices = payload.get("pairedDevices", [])
        if not isinstance(devices, list):
            return []
        return [
            {
                "name": item.get("deviceVersion") or item.get("displayName"),
                "device_type": item.get("deviceType"),
                "last_sync_time": item.get("lastSyncTime"),
                "features": item.get("features", []),
            }
            for item in devices
            if isinstance(item, dict)
        ]
