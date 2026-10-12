"""Package setup configuration."""

import sysconfig

import setuptools

if __name__ == "__main__":
    if sysconfig.get_platform().startswith(("win", "macosx-")):
        from setuptools_rust import Binding, RustExtension

        setuptools.setup(
            rust_extensions=[
                RustExtension(
                    "serialx._serialx_rust",
                    binding=Binding.PyO3,
                    optional=True,
                )
            ]
        )
    else:
        setuptools.setup()
