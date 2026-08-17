from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Iterable


class JobState(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"
    QUEUED = "queued"
    WAITING_FOR_GPU = "waiting_for_gpu"
    UNLOADING = "unloading"
    VERIFYING_VRAM = "verifying_vram"
    LOADING = "loading"
    WARMING = "warming"
    RUNNING = "running"
    SAVING = "saving"
    COMPLETED = "completed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"
    RECOVERY_REQUIRED = "recovery_required"


TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.CANCELLED,
    JobState.FAILED,
    JobState.EXPIRED,
}

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.CREATED: {JobState.VALIDATED, JobState.FAILED, JobState.CANCELLED},
    JobState.VALIDATED: {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED},
    JobState.QUEUED: {JobState.WAITING_FOR_GPU, JobState.RUNNING, JobState.CANCELLING, JobState.FAILED},
    JobState.WAITING_FOR_GPU: {JobState.UNLOADING, JobState.LOADING, JobState.CANCELLING, JobState.FAILED},
    JobState.UNLOADING: {JobState.VERIFYING_VRAM, JobState.RECOVERY_REQUIRED, JobState.CANCELLING, JobState.FAILED},
    JobState.VERIFYING_VRAM: {JobState.LOADING, JobState.RECOVERY_REQUIRED, JobState.CANCELLING, JobState.FAILED},
    JobState.LOADING: {JobState.WARMING, JobState.RECOVERY_REQUIRED, JobState.CANCELLING, JobState.FAILED},
    JobState.WARMING: {JobState.RUNNING, JobState.CANCELLING, JobState.FAILED},
    JobState.RUNNING: {JobState.SAVING, JobState.CANCELLING, JobState.FAILED, JobState.RECOVERY_REQUIRED},
    JobState.SAVING: {JobState.COMPLETED, JobState.FAILED, JobState.RECOVERY_REQUIRED},
    JobState.CANCELLING: {JobState.CANCELLED, JobState.FAILED, JobState.RECOVERY_REQUIRED},
    JobState.RECOVERY_REQUIRED: {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED},
}


class PriorityClass(str, Enum):
    CHAT = "chat"
    INTERACTIVE_AUDIO = "interactive_audio"
    SINGLE_IMAGE = "single_image"
    IMAGE_BATCH = "image_batch"
    VIDEO = "video"
    BATCH = "batch"


PRIORITY_BASE_SCORE = {
    PriorityClass.CHAT: 0,
    PriorityClass.INTERACTIVE_AUDIO: 10,
    PriorityClass.SINGLE_IMAGE: 20,
    PriorityClass.IMAGE_BATCH: 30,
    PriorityClass.VIDEO: 40,
    PriorityClass.BATCH: 50,
}
SAME_MODEL_GROUPING_SCORE_WINDOW = 3.0
CPU_RESIDENT_DEFAULT_ALIASES = ("embedding-default", "tts-fast", "stt-default")


@dataclass(frozen=True)
class ResourcePolicy:
    gpu_total_vram_gib: float = 12.0
    gpu_usable_vram_gib: float = 10.5
    gpu_reserve_vram_gib: float = 1.5
    gpu_max_active_pipelines: int = 1
    host_total_ram_gib: float = 32.0
    host_reserve_ram_gib: float = 6.0
    llm_default_context: int = 8192
    llm_maximum_context: int = 16384
    llm_default_parallel_requests: int = 1
    comfyui_maximum_parallel_jobs: int = 1
    comfyui_maximum_batch_size: int = 1
    cpu_residency_enabled: bool = True
    cpu_residency_max_ram_gib: float = 2.0
    cpu_resident_aliases: tuple[str, ...] = CPU_RESIDENT_DEFAULT_ALIASES

    @property
    def host_usable_ram_gib(self) -> float:
        return max(0.0, self.host_total_ram_gib - self.host_reserve_ram_gib)

    @property
    def cpu_residency_usable_ram_gib(self) -> float:
        return min(self.host_usable_ram_gib, max(0.0, self.cpu_residency_max_ram_gib))


@dataclass(frozen=True)
class ResourceEstimate:
    vram_gib: float
    ram_gib: float
    disk_gib: float = 0.0
    requires_gpu: bool = True


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    label: str
    reason: str


@dataclass(frozen=True)
class CpuResidencyDecision:
    candidate: bool
    allowed: bool
    reason: str


@dataclass(frozen=True)
class QueueItem:
    job_id: str
    priority: PriorityClass
    queued_at: datetime
    model_alias: str
    runtime: str = ""
    resolved_model_version: str = ""


def validate_transition(current: JobState, target: JobState) -> bool:
    if current in TERMINAL_STATES:
        return False
    return target in ALLOWED_TRANSITIONS.get(current, set())


def classify_resource_fit(policy: ResourcePolicy, estimate: ResourceEstimate) -> AdmissionDecision:
    if estimate.vram_gib > policy.gpu_total_vram_gib:
        return AdmissionDecision(False, "incompatible", "estimated VRAM exceeds physical GPU memory")
    if estimate.ram_gib > policy.host_total_ram_gib:
        return AdmissionDecision(False, "incompatible", "estimated RAM exceeds physical host memory")
    if estimate.vram_gib > policy.gpu_usable_vram_gib:
        return AdmissionDecision(True, "offload-required", "estimated VRAM exceeds default usable VRAM")
    if estimate.ram_gib > policy.host_usable_ram_gib:
        return AdmissionDecision(True, "experimental", "estimated RAM consumes reserved host memory")
    if estimate.vram_gib <= policy.gpu_usable_vram_gib * 0.75 and estimate.ram_gib <= policy.host_usable_ram_gib * 0.75:
        return AdmissionDecision(True, "recommended", "estimate is inside the RTX 3060/32 GB profile")
    return AdmissionDecision(True, "expected", "estimate is within policy but close to limits")


def classify_cpu_residency(policy: ResourcePolicy, model_alias: str, estimate: ResourceEstimate) -> CpuResidencyDecision:
    if estimate.requires_gpu:
        return CpuResidencyDecision(False, False, "GPU-backed models are managed by the GPU idle-unload policy")
    if not policy.cpu_residency_enabled:
        return CpuResidencyDecision(True, False, "CPU residency is disabled by policy")
    if model_alias not in set(policy.cpu_resident_aliases):
        return CpuResidencyDecision(True, False, "alias is not in the CPU residency allowlist")
    if estimate.ram_gib > policy.host_usable_ram_gib:
        return CpuResidencyDecision(True, False, "estimated resident RAM would consume the protected host reserve")
    if estimate.ram_gib > policy.cpu_residency_usable_ram_gib:
        return CpuResidencyDecision(True, False, "estimated resident RAM exceeds the CPU residency cap")
    return CpuResidencyDecision(True, True, "eligible to remain resident without consuming GPU VRAM or protected host RAM")


def queue_sort_key(item: QueueItem, now: datetime | None = None) -> tuple[float, datetime, str]:
    current = now or datetime.now(tz=UTC)
    age_minutes = max(0.0, (current - item.queued_at).total_seconds() / 60.0)
    aging_bonus = age_minutes / 5.0
    score = PRIORITY_BASE_SCORE[item.priority] - aging_bonus
    return (score, item.queued_at, item.job_id)


def queue_item_matches_active_model(item: QueueItem, active_runtime_model_refs: set[tuple[str, str]]) -> bool:
    return bool(item.runtime and item.resolved_model_version and (item.runtime, item.resolved_model_version) in active_runtime_model_refs)


def select_next_job(
    items: Iterable[QueueItem],
    now: datetime | None = None,
    active_runtime_model_refs: set[tuple[str, str]] | None = None,
    same_model_score_window: float = SAME_MODEL_GROUPING_SCORE_WINDOW,
) -> QueueItem | None:
    candidates = list(items)
    sorted_items = sorted(candidates, key=lambda item: queue_sort_key(item, now))
    if not sorted_items:
        return None
    selected = sorted_items[0]
    active_refs = active_runtime_model_refs or set()
    if not active_refs:
        return selected
    same_model_candidates = [item for item in sorted_items if queue_item_matches_active_model(item, active_refs)]
    if not same_model_candidates:
        return selected
    same_model_selected = same_model_candidates[0]
    selected_score = queue_sort_key(selected, now)[0]
    same_model_score = queue_sort_key(same_model_selected, now)[0]
    if same_model_score <= selected_score + max(0.0, same_model_score_window):
        return same_model_selected
    return selected
