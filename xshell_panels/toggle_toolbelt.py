"""Bật/tắt Toolbelt (chứa panel Xshell) qua menu iTerm2. Chạy bởi mục Scripts > Toggle XShell."""
import iterm2


async def main(connection):
    await iterm2.MainMenu.async_select_menu_item(connection, "Show Toolbelt")


if __name__ == "__main__":
    iterm2.run_until_complete(main)
