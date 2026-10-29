import unittest

from jobs.queue import JobQueue


class Unorderable:
    """Jobs need not be comparable."""


class HiddenQueueTest(unittest.TestCase):
    def test_unorderable_jobs(self):
        q = JobQueue()
        a, b = Unorderable(), Unorderable()
        q.push(1, a)
        q.push(1, b)
        self.assertIs(q.pop(), a)
        self.assertIs(q.pop(), b)

    def test_interleaved(self):
        q = JobQueue()
        q.push(2, "b1")
        q.push(1, "a1")
        q.push(2, "b2")
        q.push(1, "a2")
        self.assertEqual([q.pop() for _ in range(4)], ["a1", "a2", "b1", "b2"])
        self.assertEqual(len(q), 0)
