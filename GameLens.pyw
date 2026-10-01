"""Double-click launcher; use the installed shortcut for the project environment."""
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from gamelens.desktop import main
    raise SystemExit(main())
