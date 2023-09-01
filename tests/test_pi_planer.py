import unittest

from accounting_bot.universe import pi_planer
from accounting_bot.universe.pi_planer import PiPlaner, Planet, Array


class PiPlanerTest(unittest.TestCase):

    def assertPlanEqual(self, expected: PiPlaner, actual: PiPlaner):
        self.assertEqual(len(expected.arrays), len(actual.arrays),
                         msg=f"Expected {len(expected.arrays)}, but got {len(actual.arrays)} arrays")
        used = []
        for array in expected.arrays:
            found = False
            for a in actual.arrays:
                if (
                        array.planet.id == a.planet.id and
                        array.resource_id == a.resource_id and
                        array.locked == a.locked and
                        a not in used):
                    found = True
                    used.append(a)
                    break
            self.assertTrue(found,
                            msg=f"Expected array {array.planet.id}:{array.resource_id}:{array.locked} "
                                f"but did not found it")

    @classmethod
    def setUpClass(cls) -> None:
        pi_planer.pi_ids = {
            "Lustering Alloy": 42001000000,
            "Sheen Compound": 42001000001,
            "Gleaming Alloy": 42001000002,
            "Condensed Alloy": 42001000003,
            "Precious Alloy": 42001000004,
            "Motley Compound": 42001000005,
            "Fiber Composite": 42001000006,
            "Lucent Compound": 42001000007,
            "Opulent Compound": 42001000008,
            "Glossy Compound": 42001000009,
            "Crystal Compound": 42001000010,
            "Dark Compound": 42001000011,
            "Reactive Gas": 42001000018,
            "Noble Gas": 42001000019,
            "Base Metals": 42001000020,
            "Heavy Metals": 42001000021,
            "Noble Metals": 42001000022,
            "Reactive Metals": 42001000023,
            "Toxic Metals": 42001000024,
            "Industrial Fibers": 42001000025,
            "Supertensile Plastics": 42001000026,
            "Polyaramids": 42001000027,
            "Coolant": 42001000028,
            "Condensates": 42001000029,
            "Construction Blocks": 42001000030,
            "Nanites": 42001000031,
            "Silicate Glass": 42001000032,
            "Smartfab Units": 42001000033,
            "Heavy Water": 42002000012,
            "Suspended Plasma": 42002000013,
            "Liquid Ozone": 42002000014,
            "Ionic Solutions": 42002000015,
            "Oxygen Isotopes": 42002000016,
            "Plasmoids": 42002000017
        }

    def test_item_id_integrity(self):
        short_i = 0
        for i in range(42001000000, 42001000012):
            i_enc = pi_planer._encode_item_id(i)
            self.assertEqual(short_i, i_enc, msg=f"Item id encoding for first pi group failed for id {i}")
            self.assertEqual(i, pi_planer._decode_item_id(i_enc),
                             msg=f"Item id decoding for first pi group failed, encoded: {i_enc}")
            short_i += 1
        for i in range(42001000018, 42001000034):
            i_enc = pi_planer._encode_item_id(i)
            self.assertEqual(short_i, i_enc, msg=f"Item id encoding for second pi group failed for id {i}")
            self.assertEqual(i, pi_planer._decode_item_id(i_enc),
                             msg=f"Item id decoding for second pi group failed, encoded: {i_enc}")
            short_i += 1
        for i in range(42002000012, 42002000018):
            i_enc = pi_planer._encode_item_id(i)
            self.assertEqual(short_i, i_enc, msg=f"Item id encoding for fuel pi group failed for id {i}")
            self.assertEqual(i, pi_planer._decode_item_id(i_enc),
                             msg=f"Item id decoding for fuel pi group failed, encoded: {i_enc}")
            short_i += 1

    def test_encode(self):
        plan = PiPlaner(arrays=5, planets=6)
        p1 = Planet(p_id=40000011, name="P1")
        p2 = Planet(p_id=40001234, name="P2")
        p3 = Planet(p_id=40009999, name="P3")
        p4 = Planet(p_id=40099999, name="P4")
        p5 = Planet(p_id=40012345, name="P5")
        p6 = Planet(p_id=40012345, name="P6")

        a1 = Array(resource="Lustering Alloy")
        a1.resource_id = 42001000000
        a1.planet = p1

        a2 = Array(resource="Dark Compound")
        a2.resource_id = 42001000011
        a2.planet = p2
        a2.locked = True

        a3 = Array(resource="Reactive Gas")
        a3.resource_id = 42001000018
        a3.planet = p3

        a4 = Array(resource="Smartfab Units")
        a4.resource_id = 42001000033
        a4.planet = p4
        a4.locked = True

        a5 = Array(resource="Heavy Water")
        a5.resource_id = 42002000012
        a5.planet = p5

        a6 = Array(resource="Plasmoids")
        a6.resource_id = 42002000017
        a6.planet = p6
        a6.locked = True

        plan.arrays = [a1, a2, a3, a4, a5, a6]
        c = plan.encode_base64()
        new_plan = PiPlaner.decode_base64(c)
        self.assertPlanEqual(plan, new_plan)

    if __name__ == '__main__':
        unittest.main()
