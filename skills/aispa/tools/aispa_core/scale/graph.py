"""Iterative dependency index shared by all checks of one proposed plan."""
import sys

from ..store import Invalid


class DependencyIndex:
    def __init__(self, tasks, *, budget=None):
        self.tasks = {task['id']: task for task in tasks}
        if len(self.tasks) != len(tasks):
            raise Invalid('Повторный ID задачи')
        self.order, done = [], set()
        for root in self.tasks:
            if root in done:
                continue
            stack, visiting = [(root, False)], set()
            while stack:
                if budget:
                    budget.check(0, path="dependency DAG validation", progress=len(done), progress_unit="items")
                tid, leaving = stack.pop()
                if leaving:
                    visiting.remove(tid)
                    done.add(tid)
                    self.order.append(tid)
                    continue
                if tid not in self.tasks:
                    raise Invalid('Неизвестная зависимость: ' + tid)
                if tid in visiting:
                    raise Invalid('Цикл зависимостей: ' + tid)
                if tid in done:
                    continue
                visiting.add(tid)
                stack.append((tid, True))
                stack.extend((dep, False) for dep in reversed(self.tasks[tid]['dependsOn']))
        self.positions = {tid: i for i, tid in enumerate(self.order)}
        self.bits = {}
        mask_bytes = 0
        index_bytes = sys.getsizeof(self.tasks) + sys.getsizeof(self.order) + sys.getsizeof(self.positions)
        for tid in self.order:
            position = self.positions[tid]
            if budget:
                # Every ancestor position is lower than this topological index.
                # Check the next mask's upper bound BEFORE allocating its int.
                digits = (position + sys.int_info.bits_per_digit) // sys.int_info.bits_per_digit
                projected = sys.getsizeof(0) + digits * sys.int_info.sizeof_digit
                budget.check_memory(index_bytes + sys.getsizeof(self.bits) + mask_bytes + projected,
                                    path="dependency ancestor masks", progress=len(self.bits), progress_unit="items")
            bits = 1 << position
            for dep in self.tasks[tid]['dependsOn']:
                bits |= self.bits[dep]
            self.bits[tid] = bits
            mask_bytes += sys.getsizeof(bits)
            if budget:
                budget.check_memory(index_bytes + sys.getsizeof(self.bits) + mask_bytes,
                                    path="dependency ancestor masks", progress=len(self.bits), progress_unit="items")

    def contains(self, tid, ancestor):
        return bool(self.bits[tid] & (1 << self.positions[ancestor]))

    def closure(self, tid):
        if tid not in self.tasks:
            raise Invalid('Неизвестная зависимость: ' + tid)
        # Preserve dependency-first DFS order of the specific root, rather than
        # letting unrelated insertion order change historical contract hashes.
        result, seen = [], set()
        stack = [(tid, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                result.append(self.tasks[current])
            elif current not in seen:
                seen.add(current)
                stack.append((current, True))
                stack.extend((dep, False) for dep in reversed(self.tasks[current]['dependsOn']))
        return result
