//! Local, editable model inputs. These files carry no server authorization or publication authority.
use super::{
    compile::{self, VerifiedMethods},
    contracts::{Bounds, Cutoff, DatasetRef, Exchange, PreparedInput, SourceInput, StudyResult},
    source::{self, Documents},
};
use anyhow::{Context, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub const PROFILE: &str = "lca.model-local.v1";
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ModelInput {
    pub schema_version: String,
    pub model: Value,
    pub datasets: Vec<Value>,
    #[serde(default)]
    pub backgrounds: Vec<Value>,
    pub methods: Vec<DatasetRef>,
    #[serde(default = "unit_amount")]
    pub amount: f64,
    #[serde(default)]
    pub cutoffs: Vec<Cutoff>,
    #[serde(default)]
    pub limits: LocalLimits,
}
fn unit_amount() -> f64 {
    1.0
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct LocalLimits {
    pub max_input_bytes: usize,
    pub max_documents: usize,
    pub max_instances: usize,
    pub max_views: usize,
    pub max_exchanges: usize,
    pub max_dense_cells: usize,
    pub max_artifact_bytes: usize,
    pub max_memory_bytes: usize,
    pub timeout_ms: u64,
}
impl Default for LocalLimits {
    fn default() -> Self {
        let b = Bounds::default();
        Self {
            max_input_bytes: b.max_source_bytes,
            max_documents: b.max_documents,
            max_instances: b.max_instances,
            max_views: b.max_views,
            max_exchanges: b.max_exchanges,
            max_dense_cells: b.max_dense_cells,
            max_artifact_bytes: b.max_artifact_bytes,
            max_memory_bytes: b.max_owned_bytes,
            timeout_ms: b.max_elapsed_millis,
        }
    }
}
impl LocalLimits {
    pub fn bounds(&self) -> anyhow::Result<Bounds> {
        for v in [
            self.max_input_bytes,
            self.max_documents,
            self.max_instances,
            self.max_views,
            self.max_exchanges,
            self.max_dense_cells,
            self.max_artifact_bytes,
            self.max_memory_bytes,
        ] {
            ensure!(
                v > 0 && isize::try_from(v).is_ok(),
                "local_resource_setting_invalid"
            );
        }
        ensure!(self.timeout_ms > 0, "local_timeout_invalid");
        Ok(Bounds {
            max_source_bytes: self.max_input_bytes,
            max_documents: self.max_documents,
            max_instances: self.max_instances,
            max_views: self.max_views,
            max_exchanges: self.max_exchanges,
            max_dense_cells: self.max_dense_cells,
            max_nonzeros: self.max_dense_cells,
            max_artifact_bytes: self.max_artifact_bytes,
            max_owned_bytes: self.max_memory_bytes,
            max_elapsed_millis: self.timeout_ms,
            ..Bounds::default()
        })
    }
}
fn text(v: &Value) -> anyhow::Result<String> {
    v.as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .context("local_required_text")
}
fn identity(v: &Value) -> anyhow::Result<DatasetRef> {
    Ok(DatasetRef {
        id: text(&v["id"])?,
        version: text(&v["version"])?,
    })
}
fn document(value: &Value) -> anyhow::Result<((String, String, String), Value)> {
    for (kind, root, info) in [
        (
            "lifecyclemodel",
            "lifeCycleModelDataSet",
            "lifeCycleModelInformation",
        ),
        ("process", "processDataSet", "processInformation"),
        ("flow", "flowDataSet", "flowInformation"),
        (
            "flowproperty",
            "flowPropertyDataSet",
            "flowPropertiesInformation",
        ),
        ("unitgroup", "unitGroupDataSet", "unitGroupInformation"),
    ] {
        if !value[root].is_null() {
            let id = text(&value[root][info]["dataSetInformation"]["common:UUID"])?;
            let version = text(
                &value[root]["administrativeInformation"]["publicationAndOwnership"]["common:dataSetVersion"],
            )?;
            return Ok(((kind.into(), id, version), value.clone()));
        }
    }
    anyhow::bail!("local_dataset_kind")
}
fn background_exchange(
    key: &Value,
    amount: f64,
    id: String,
    kind: &str,
) -> anyhow::Result<Exchange> {
    ensure!(amount.is_finite(), "local_nonfinite_inventory");
    let direction = text(&key["direction"])?;
    ensure!(
        direction == "Input" || direction == "Output",
        "local_inventory_direction"
    );
    let unit = &key["unit"];
    Ok(Exchange {
        id,
        flow: DatasetRef {
            id: text(&key["flowId"])?,
            version: text(&key["flowVersion"])?,
        },
        direction,
        amount,
        kind: kind.into(),
        unit: text(&unit["name"])?,
        unit_identity: vec![
            DatasetRef {
                id: text(&unit["flowPropertyId"])?,
                version: text(&unit["flowPropertyVersion"])?,
            },
            DatasetRef {
                id: text(&unit["unitGroupId"])?,
                version: text(&unit["unitGroupVersion"])?,
            },
        ],
        allocations: None,
    })
}
fn background(value: &Value) -> anyhow::Result<(DatasetRef, Vec<Exchange>)> {
    ensure!(
        value["schemaVersion"] == 1 && value["kind"] == "lci" && value["complete"] == true,
        "local_background_incomplete"
    );
    let source = identity(&value["source"])?;
    let data = &value["data"];
    let selected = &value["result"];
    let result = &data["result"];
    ensure!(
        selected["uuid"].is_string()
            && selected["uuid"] == result["uuid"]
            && selected["version"].is_string()
            && selected["version"] == result["version"],
        "local_background_result_mismatch"
    );
    let reference = &result["quantitativeReference"];
    let pivot = reference["value"]
        .as_f64()
        .context("local_background_reference")?;
    ensure!(
        pivot.is_finite() && pivot != 0.0 && reference["amountField"] == "resultingAmount",
        "local_background_reference"
    );
    let items = data["items"]
        .as_array()
        .context("local_background_inventory")?;
    ensure!(
        data["nonzeroCount"].as_u64() == Some(u64::try_from(items.len())?),
        "local_background_incomplete"
    );
    let mut exchanges = vec![background_exchange(
        reference,
        pivot,
        "reference".into(),
        "Product flow",
    )?];
    let mut seen = BTreeSet::new();
    for (index, row) in items.iter().enumerate() {
        let key = &row["key"];
        ensure!(
            seen.insert(serde_json::to_vec(key)?),
            "local_background_duplicate_inventory"
        );
        // LCI is already allocated/aggregated. Retain signed amounts and the published reference basis.
        exchanges.push(background_exchange(
            key,
            row["amount"].as_f64().context("local_inventory_amount")?,
            format!("lci_{index}"),
            "Elementary flow",
        )?);
    }
    Ok((source, exchanges))
}
/// Compile editable local data without pretending it is a server-issued `SourceInput`.
pub fn prepare(
    bytes: &[u8],
    input: &ModelInput,
    methods: &VerifiedMethods,
) -> anyhow::Result<PreparedInput> {
    let started = std::time::Instant::now();
    ensure!(
        input.schema_version == "lca.model-input.v1",
        "local_input_schema"
    );
    let bounds = input.limits.bounds()?;
    ensure!(
        bytes.len() <= bounds.max_source_bytes
            && bytes.len().saturating_mul(8) <= bounds.max_owned_bytes,
        "local_input_budget"
    );
    ensure!(
        input.datasets.len().saturating_add(1) <= bounds.max_documents,
        "local_document_limit"
    );
    ensure!(
        methods
            .axes
            .iter()
            .map(|m| DatasetRef {
                id: m.id.clone(),
                version: m.version.clone()
            })
            .collect::<Vec<_>>()
            == input.methods,
        "local_method_selection"
    );
    ensure!(
        !input.methods.is_empty()
            && input.methods.len() <= bounds.max_methods
            && input
                .methods
                .iter()
                .map(|m| &m.id)
                .collect::<BTreeSet<_>>()
                .len()
                == input.methods.len(),
        "local_method_selection"
    );
    let (model_key, model) = document(&input.model)?;
    ensure!(model_key.0 == "lifecyclemodel", "local_model_required");
    let origin = DatasetRef {
        id: model_key.1.clone(),
        version: model_key.2.clone(),
    };
    let mut docs = Documents::from([(model_key, model)]);
    for value in &input.datasets {
        let (key, value) = document(value)?;
        ensure!(docs.insert(key, value).is_none(), "local_duplicate_dataset");
    }
    let mut backgrounds = BTreeMap::new();
    for value in &input.backgrounds {
        let (source, exchanges) = background(value)?;
        ensure!(
            backgrounds
                .insert((source.id, source.version), exchanges)
                .is_none(),
            "local_duplicate_background"
        );
    }
    // This internal context supplies graph settings only: no state, owner, source document or hash claims.
    let context = SourceInput {
        schema_version: super::contracts::SOURCE_SCHEMA.into(),
        origin,
        actor_id: None,
        documents: vec![],
        method_refs: input.methods.clone(),
        cutoffs: input.cutoffs.clone(),
        overrides: vec![],
        study_binding: None,
        quality_findings: vec![],
    };
    let (model, owned) = source::normalize_graph(
        &context,
        &docs,
        vec![],
        &backgrounds,
        &bounds,
        bytes.len().saturating_mul(8),
    )?;
    for key in backgrounds.keys() {
        ensure!(
            model
                .instances
                .iter()
                .any(|i| (&i.process.id, &i.process.version) == (&key.0, &key.1)),
            "local_unused_background"
        );
    }
    compile::compile_model(
        bytes,
        &context,
        model,
        owned,
        methods,
        &bounds,
        Some(input.amount),
        started,
    )
}
pub fn execute(input: &PreparedInput) -> anyhow::Result<StudyResult> {
    let started = std::time::Instant::now();
    compile::validate_execution_profile(input, PROFILE)?;
    compile::execute_validated(input, started)
}
