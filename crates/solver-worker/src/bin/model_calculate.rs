//! Standalone local Model calculation. No server credentials, queue or provider discovery.
use clap::Parser;
use solver_worker::{
    static_lcia_cache::{TrustedStaticCacheSource, load_verified_static_lcia_bundle},
    study::{
        VerifiedMethods, artifact_bytes,
        local::{self, ModelInput},
    },
};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    version,
    about = "Calculate a complete local Model input without database or queue access"
)]
struct Args {
    #[arg(long)]
    input: Option<PathBuf>,
    #[arg(long)]
    static_cache: Option<PathBuf>,
    #[arg(long)]
    output_dir: Option<PathBuf>,
    #[arg(long)]
    check: bool,
    #[arg(long)]
    describe: bool,
    #[arg(long, default_value_t = 4 * 1024 * 1024)]
    max_input_bytes: u64,
}
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    anyhow::ensure!(!(args.check && args.describe), "local_mode_conflict");
    if args.describe {
        anyhow::ensure!(
            args.input.is_none() && args.output_dir.is_none() && args.static_cache.is_none(),
            "local_mode_conflict"
        );
        println!(
            "{}",
            serde_json::json!({"schemaVersion":1,"engine":"tiangong-native","engineVersion":env!("CARGO_PKG_VERSION"),"profile":local::PROFILE,"inputSchema":"lca.model-input.v1","characterization":"method-defined-sparse","limits":local::LocalLimits::default()})
        );
        return Ok(());
    }
    let input = if args.check {
        anyhow::ensure!(
            args.input.is_none() && args.output_dir.is_none(),
            "local_mode_conflict"
        );
        None
    } else {
        let input_path = args
            .input
            .clone()
            .ok_or_else(|| anyhow::anyhow!("local_input_required"))?;
        let output_dir = args
            .output_dir
            .clone()
            .ok_or_else(|| anyhow::anyhow!("local_output_required"))?;
        anyhow::ensure!(!output_dir.exists(), "local_output_exists");
        anyhow::ensure!(
            std::fs::metadata(&input_path)?.len() <= args.max_input_bytes,
            "local_input_bytes_limit"
        );
        let bytes = std::fs::read(&input_path)?;
        anyhow::ensure!(
            u64::try_from(bytes.len())? <= args.max_input_bytes,
            "local_input_bytes_limit"
        );
        let input: ModelInput = solver_worker::study::json::from_slice(&bytes)?;
        input.limits.bounds()?;
        Some((bytes, input, output_dir))
    };
    let cache_directory = args
        .static_cache
        .ok_or_else(|| anyhow::anyhow!("local_static_cache_required"))?;
    let request = solver_worker::calculation_evidence::method_factor_source_contract_fixture();
    let cache = load_verified_static_lcia_bundle(
        &TrustedStaticCacheSource::Directory(cache_directory),
        &request,
    )
    .await?;
    if args.check {
        println!(
            "{}",
            serde_json::json!({"schemaVersion":1,"status":"ready","engine":"tiangong-native","engineVersion":env!("CARGO_PKG_VERSION"),"profile":local::PROFILE,"inputSchema":"lca.model-input.v1","characterization":"method-defined-sparse","methodCount":cache.methods.len(),"limits":local::LocalLimits::default()})
        );
        return Ok(());
    }
    let (bytes, input, output_dir) =
        input.ok_or_else(|| anyhow::anyhow!("local_input_required"))?;
    let methods = VerifiedMethods::from_static(&cache, &input.methods)?;
    let prepared = local::prepare(&bytes, &input, &methods)?;
    let result = local::execute(&prepared)?;
    write_run(&output_dir, &bytes, &input, &prepared, &result)?;
    Ok(())
}

fn write_run(
    output_dir: &std::path::Path,
    bytes: &[u8],
    input: &ModelInput,
    prepared: &solver_worker::study::contracts::PreparedInput,
    result: &solver_worker::study::contracts::StudyResult,
) -> anyhow::Result<()> {
    let summary = serde_json::json!({
        "schemaVersion": 1, "status": "completed", "engine": "tiangong-native",
        "engineVersion": env!("CARGO_PKG_VERSION"), "profile": local::PROFILE,
        "amount": input.amount, "activityCount": result.x.len(), "inventoryCount": result.g.len(),
        "impactCount": result.h.len(), "coverageGapCount": result.coverage.len(),
        "completeLciaScores": result.complete_lcia_scores, "maxResidual": result.max_residual,
        "elapsedMillis": result.elapsed_millis,
        "files": {"input":"input.json", "prepared":"prepared.json", "result":"result.json"}
    });
    let parent = output_dir
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| std::path::Path::new("."));
    let staging = tempfile::Builder::new()
        .prefix(".model-calculation-")
        .tempdir_in(parent)?;
    std::fs::write(staging.path().join("input.json"), bytes)?;
    std::fs::write(
        staging.path().join("prepared.json"),
        artifact_bytes(prepared)?,
    )?;
    std::fs::write(
        staging.path().join("result.json"),
        serde_json::to_vec_pretty(result)?,
    )?;
    std::fs::write(
        staging.path().join("summary.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    // Claim an empty target exclusively before publishing files; never replace an existing run.
    let mut directory = std::fs::DirBuilder::new();
    directory.recursive(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        directory.mode(0o700);
    }
    directory.create(output_dir)?;
    for name in ["input.json", "prepared.json", "result.json", "summary.json"] {
        std::fs::rename(staging.path().join(name), output_dir.join(name))?;
    }
    println!("{summary}");
    Ok(())
}
