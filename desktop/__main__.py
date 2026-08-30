from pipeline.hidden_process import install_hidden_subprocess

install_hidden_subprocess()

from desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
