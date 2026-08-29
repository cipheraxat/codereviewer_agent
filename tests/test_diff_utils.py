from codereview.diff_utils import line_for_pattern


def test_line_for_pattern_finds_added_line() -> None:
    patch = """@@ -1,3 +1,5 @@
 def login(username):
     if not username:
         return False
+    api_key = "secret"
+    print("debug", username)
     return True
"""
    assert line_for_pattern(patch, r"api_key") == 4
    assert line_for_pattern(patch, r"print\(") == 5
