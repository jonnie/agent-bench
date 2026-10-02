"""Built-in small-feature benchmark fixture. Candidate source is data."""

from .base import create_task

TASK = create_task(
    "small-feature",
    "Add nested configuration overlays and deletion",
    """
        Extend solution.settings.merge_settings(base, override) for layered service
        configuration, and export a singleton sentinel solution.settings.DELETE.
        Inputs are dicts with string keys, JSON-like values, and DELETE allowed
        as a direct dict value in override (including nested override dicts).
        For each override key: DELETE removes that key if present; dict values
        recursively merge when the base value is a dict, otherwise merge into an
        empty dict; all other values replace the base value. Lists replace rather
        than concatenate, and None is an ordinary replacement, not deletion.
        An absent deleted key is a no-op. Preserve unmentioned base keys.
        Return a completely independent deep copy: neither input may be mutated,
        and no mutable dict/list in the result may alias either input. DELETE
        must never appear in the result. Base never contains DELETE; lists never
        contain DELETE. Keep the two-argument API and support empty mappings.
        Run public tests with python -m unittest discover -s tests.
        """,
    {
        "settings.py": """
            DELETE = object()

            def merge_settings(base, override):
                result = base.copy()
                result.update(override)
                return result
        """
    },
    """
        import unittest
        from solution.settings import DELETE, merge_settings

        class SettingsTests(unittest.TestCase):
            def test_flat_compatibility(self):
                self.assertEqual(merge_settings({'port': 80}, {'port': 8080}), {'port': 8080})

            def test_nested_overlay(self):
                self.assertEqual(merge_settings({'db': {'host': 'local', 'password': 'old'}},
                                                {'db': {'password': DELETE, 'port': 5432}}),
                                 {'db': {'host': 'local', 'port': 5432}})
        """,
    """
        import copy
        import random
        import unittest
        from solution.settings import DELETE, merge_settings

        def reference(base, override):
            out = copy.deepcopy(base)
            for key, value in override.items():
                if value is DELETE:
                    out.pop(key, None)
                elif isinstance(value, dict):
                    out[key] = reference(base.get(key, {}) if isinstance(base.get(key), dict) else {}, value)
                else:
                    out[key] = copy.deepcopy(value)
            return out

        class SettingsContract(unittest.TestCase):
            def test_flat_and_empty(self):
                self.assertEqual(merge_settings({}, {}), {})
                self.assertEqual(merge_settings({'a': 1, 'b': 2}, {'a': None}), {'a': None, 'b': 2})

            def test_nested_delete_and_type_changes(self):
                base = {'db': {'host': 'localhost', 'password': 'secret'}, 'cache': False}
                override = {'db': {'password': DELETE, 'missing': DELETE},
                            'cache': {'ttl': 60, 'gone': DELETE}, 'absent': DELETE}
                self.assertEqual(merge_settings(base, override),
                                 {'db': {'host': 'localhost'}, 'cache': {'ttl': 60}})
                self.assertEqual(merge_settings({'a': {'b': 1}}, {'a': [2]}), {'a': [2]})

            def test_lists_replace_and_all_mutables_are_detached(self):
                base = {'untouched': {'items': [{'x': 1}]}, 'replaced': [1, 2]}
                override = {'replaced': [{'v': []}], 'new': {'values': [4]}}
                before_base, before_override = copy.deepcopy(base), copy.deepcopy(override)
                out = merge_settings(base, override)
                self.assertEqual(out, reference(base, override))
                out['untouched']['items'][0]['x'] = 9
                out['replaced'][0]['v'].append(8)
                out['new']['values'].append(5)
                self.assertEqual(base, before_base)
                self.assertEqual(override, before_override)

            def test_seeded_nested_configs(self):
                rng = random.Random(5417)
                def tree(depth, deletions=False):
                    out = {}
                    for key in rng.sample(['a', 'b', 'c', 'd'], rng.randrange(5)):
                        kind = rng.randrange(5 if deletions else 4)
                        if kind == 0 and depth:
                            out[key] = tree(depth - 1, deletions)
                        elif kind == 1:
                            out[key] = [{'value': rng.randrange(10)}]
                        elif kind == 2:
                            out[key] = None
                        elif kind == 4:
                            out[key] = DELETE
                        else:
                            out[key] = rng.randrange(100)
                    return out
                for _ in range(70):
                    base, override = tree(3), tree(3, True)
                    self.assertEqual(merge_settings(base, override), reference(base, override))
        """,
)
