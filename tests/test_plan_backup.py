"""Restore's dynamic table comparison includes both private plan tables."""

from support.backup_scripts import comparisons, run
from support.backup_scripts import count_docker as count_docker
from support.backup_scripts import pytestmark as pytestmark
from support.backup_scripts import sandbox as sandbox


def test_restore_counts_plan_versions_and_replaceable_state(count_docker):
    env, dump = count_docker
    env.update(
        {
            "RESTORED_COUNTS": "plan_versions\t2\nplan_state\t1\n",
            "LIVE_COUNTS": "plan_versions\t3\nplan_state\t1\n",
        }
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout) == {
        "plan_versions": ["2", "3", "different"],
        "plan_state": ["1", "1", "equal"],
    }
