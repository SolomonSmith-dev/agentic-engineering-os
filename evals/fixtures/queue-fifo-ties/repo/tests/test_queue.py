import unittest

from jobs.queue import JobQueue


class QueueTest(unittest.TestCase):
    def test_priority_order(self):
        q = JobQueue()
        q.push(5, "late")
        q.push(1, "early")
        self.assertEqual(q.pop(), "early")
        self.assertEqual(q.pop(), "late")

    def test_empty(self):
        with self.assertRaises(IndexError):
            JobQueue().pop()

    def test_equal_priority_is_fifo(self):
        q = JobQueue()
        for name in ("zeta", "alpha", "mid"):
            q.push(2, name)
        self.assertEqual([q.pop(), q.pop(), q.pop()], ["zeta", "alpha", "mid"])
