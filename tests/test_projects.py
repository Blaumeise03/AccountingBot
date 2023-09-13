import random
import string
import unittest

from accounting_bot.ext.sheet import projects
from accounting_bot.universe.data_utils import Item


class ProjectsTest(unittest.TestCase):
    def test_contract_hash(self):
        different = []
        for _ in range(10):
            items1 = []
            for _ in range(10):
                items1.append(Item(
                    name="".join(random.choice(string.ascii_letters) for _ in range(random.randint(1, 12))),
                    amount=random.randint(1, 1000000000)
                ))
            different.append(items1)
            # Test shuffled list with same items
            items2 = random.sample(items1, len(items1))
            items3 = random.sample(items1, len(items1))
            items4 = random.sample(items1, len(items1))
            items_hash = projects.hash_contract(items1)
            self.assertEqual(items_hash, projects.hash_contract(items2))
            self.assertEqual(items_hash, projects.hash_contract(items3))
            self.assertEqual(items_hash, projects.hash_contract(items4))
        # Test different lists
        for a, b in zip(different, different[1:]):
            self.assertNotEqual(projects.hash_contract(a), projects.hash_contract(b))


if __name__ == '__main__':
    unittest.main()
