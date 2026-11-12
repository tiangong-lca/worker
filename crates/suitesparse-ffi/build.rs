fn main() {
    println!("cargo:rerun-if-env-changed=PKG_CONFIG_PATH");
    println!("cargo:rerun-if-env-changed=SUITESPARSE_LIB_DIR");

    // MSVC import libraries do not require Unix BLAS/LAPACK linker names.
    // The package producer carries the DLL dependency closure alongside the exe.
    if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        let directory = std::env::var("SUITESPARSE_LIB_DIR").expect(
            "Set SUITESPARSE_LIB_DIR to the x64 dynamic SuiteSparse import-library directory",
        );
        assert!(
            std::path::Path::new(&directory)
                .join("umfpack.lib")
                .is_file(),
            "SUITESPARSE_LIB_DIR must contain the dynamic umfpack.lib import library"
        );
        println!("cargo:rustc-link-search=native={directory}");
        println!("cargo:rustc-link-lib=dylib=umfpack");
        return;
    }

    if pkg_config::Config::new()
        .cargo_metadata(true)
        .probe("UMFPACK")
        .is_ok()
    {
        return;
    }

    // Fallback linker directives for environments without UMFPACK pkg-config metadata.
    for lib in [
        "umfpack",
        "cholmod",
        "amd",
        "colamd",
        "suitesparseconfig",
        "blas",
        "lapack",
    ] {
        println!("cargo:rustc-link-lib={lib}");
    }
}
