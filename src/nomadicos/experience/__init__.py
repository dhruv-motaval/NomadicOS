"""Experience system (BP §18, §95, §109, §168, §206, §318, §352; Phase 11).

Experience = what the system actually did (BP §317) — distinct from user
memory. Quality-scored (BP §318), deduplicated (BP §168), consolidated
(BP §109), and reusable as evidence — not unquestioned commands (BP §352).
"""

from nomadicos.experience.recorder import ExperienceRecorder, TaskExperience
from nomadicos.experience.store import ExperienceStore

__all__ = ["ExperienceRecorder", "ExperienceStore", "TaskExperience"]
