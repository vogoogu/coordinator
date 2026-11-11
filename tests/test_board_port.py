import unittest

from coordinator import board, messages


class BoardPortTaken(unittest.TestCase):
    def test_second_board_on_same_port_is_refused(self):
        first = board.make_server(port=0)
        self.addCleanup(first.server_close)
        port = first.server_address[1]
        with self.assertRaises(messages.CoordinatorError) as cm:
            board.make_server(port=port)
        self.assertIn(str(port), cm.exception.what)
        self.assertTrue(cm.exception.why and cm.exception.fix)


if __name__ == "__main__":
    unittest.main()
