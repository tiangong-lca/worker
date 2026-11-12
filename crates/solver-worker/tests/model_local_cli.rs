//! Native entrypoint admission is independent of queue/database configuration.
use std::process::Command;

fn command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_model_calculate"));
    command.env_clear();
    command
}

#[test]
fn describe_is_data_free_and_exposes_actual_local_capability() {
    let output = command().arg("--describe").output().expect("describe");
    assert!(output.status.success());
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).expect("JSON");
    assert_eq!(value["profile"], "lca.model-local.v1");
    assert_eq!(value["inputSchema"], "lca.model-input.v1");
    assert_eq!(value["characterization"], "method-defined-sparse");
    assert_eq!(value["limits"]["maxInstances"], 64);
    assert_ne!(value["engineVersion"].as_str().expect("version"), "");
}

#[test]
fn setup_checks_reject_conflicting_modes_and_missing_cache_without_outputs() {
    let directory = tempfile::tempdir().expect("directory");
    for args in [
        vec!["--describe", "--check"],
        vec!["--describe", "--input", "absent"],
        vec!["--check", "--input", "absent"],
        vec!["--check"],
        vec!["--check", "--static-cache", "absent-cache"],
    ] {
        let output = command()
            .current_dir(directory.path())
            .args(args)
            .output()
            .expect("check");
        assert!(!output.status.success());
        assert_eq!(output.stdout, Vec::<u8>::new());
        assert_eq!(
            std::fs::read_dir(directory.path()).expect("files").count(),
            0
        );
    }
}

#[test]
fn invalid_run_inputs_fail_before_loading_factor_assets() {
    let output = command()
        .args(["--static-cache", "missing-cache"])
        .output()
        .expect("input");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("local_input_required"));
}
