import random
import unittest
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from accounting_bot.ext.checklist import CheckList, Task, RepeatDelay


class ChecklistTest(unittest.TestCase):

    def test_expire(self):
        # noinspection PyTypeChecker
        checklist = CheckList(plugin=None)
        now = datetime.now()
        task_valid = Task(name="valid", time=now - timedelta(days=1, hours=12), repeat=RepeatDelay.never)
        task_expired = Task(name="expired", time=now - timedelta(days=2, hours=12), repeat=RepeatDelay.never)
        task_other1 = Task(name="other3", time=now, repeat=RepeatDelay.never)
        task_other2 = Task(name="other1", time=now + timedelta(days=2, hours=12), repeat=RepeatDelay.never)
        task_other3 = Task(name="other2", time=now + timedelta(days=1, hours=12), repeat=RepeatDelay.never)
        checklist.tasks = [task_valid, task_expired, task_other1, task_other2, task_other3]
        checklist.cleanup_tasks()
        self.assertCountEqual([task_valid, task_other1, task_other2, task_other3], checklist.tasks)

    def test_refresh(self):
        # noinspection PyTypeChecker
        checklist = CheckList(plugin=None)
        now = datetime.now()
        task_never = Task(name="never", time=now - timedelta(days=1), repeat=RepeatDelay.never)
        task_daily = Task(name="daily", time=now - timedelta(days=3, hours=1), repeat=RepeatDelay.daily)
        task_weekly = Task(name="weekly", time=now - timedelta(days=3, hours=1), repeat=RepeatDelay.weekly)
        task_monthly = Task(name="monthly", time=now - timedelta(days=3, hours=1), repeat=RepeatDelay.monthly)
        # This task is only about one day expired and is not marked as finished, it should not get refreshed yet
        task_daily_pending = Task(name="daily2", time=now - timedelta(days=1, hours=1), repeat=RepeatDelay.daily)
        # This task is the same but marked as finished, it should get refreshed
        task_daily_completed = Task(name="daily3", time=now - timedelta(days=1, hours=1), repeat=RepeatDelay.daily)
        task_daily_completed.finished = Task
        checklist.tasks = [task_never, task_daily, task_weekly, task_monthly, task_daily_pending, task_daily_completed]
        checklist.cleanup_tasks()
        self.assertEqual(now - timedelta(days=1), task_never.time)
        self.assertEqual(now + timedelta(days=1, hours=-1), task_daily.time)
        self.assertEqual(now + timedelta(days=4, hours=-1), task_weekly.time)
        self.assertEqual(now + relativedelta(months=1) - timedelta(days=3, hours=1), task_monthly.time)
        self.assertEqual(now - timedelta(days=1, hours=1), task_daily_pending.time)
        self.assertEqual(now + timedelta(days=1, hours=-1), task_daily_completed.time)

    def test_sorting(self):
        # noinspection PyTypeChecker
        checklist = CheckList(plugin=None)
        now = datetime.now()
        task_a = Task(name="a", time=now + timedelta(days=1, hours=0), repeat=RepeatDelay.never)
        task_b = Task(name="b", time=now + timedelta(days=3, hours=1), repeat=RepeatDelay.daily)
        task_c = Task(name="c", time=now + timedelta(days=9, hours=11), repeat=RepeatDelay.weekly)
        task_d = Task(name="d", time=now + timedelta(days=20, hours=17), repeat=RepeatDelay.monthly)
        checklist.tasks = random.sample([task_a, task_b, task_c, task_d], 4)
        checklist.cleanup_tasks()
        self.assertListEqual([task_a, task_b, task_c, task_d], checklist.tasks)


if __name__ == '__main__':
    unittest.main()
