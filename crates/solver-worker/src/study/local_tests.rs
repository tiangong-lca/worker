use super::*;
use crate::study::local::{self, ModelInput};

fn local_input(source: SourceInput) -> ModelInput {
    let mut documents = source.documents.into_iter();
    let mut model = Value::Null;
    let mut datasets = vec![];
    for doc in documents.by_ref() {
        let value = serde_json::from_str(&doc.json).unwrap();
        if doc.kind == "lifecyclemodel" {
            model = value;
        } else {
            datasets.push(value);
        }
    }
    ModelInput {
        schema_version: "lca.model-input.v1".into(),
        model,
        datasets,
        backgrounds: vec![],
        methods: source.method_refs,
        amount: 1.0,
        cutoffs: vec![],
        limits: local::LocalLimits::default(),
    }
}
fn background_inventory() -> Value {
    let unit = json!({"flowPropertyId":"mass","flowPropertyVersion":"1","unitGroupId":"kg","unitGroupVersion":"1","internalId":"0","name":"kg"});
    let key = json!({"flowId":"emission","flowVersion":"1","direction":"Output","unit":unit});
    json!({"schemaVersion":1,"source":{"id":"bg","version":"1"},"kind":"lci","complete":true,
        "result":{"uuid":"result","version":"1"},
        "publication":{"publicationId":"provenance-only"},
        "data":{"result":{"uuid":"result","version":"1","quantitativeReference":{"flowId":"feed","flowVersion":"1","direction":"Output","unit":unit,"value":2.0,"amountField":"resultingAmount"}},
        "items":[{"key":key,"amount":6.0}],"nonzeroCount":1}})
}
fn foreground_background() -> ModelInput {
    let source = input(
        vec![
            process(
                "fg",
                vec![
                    raw_exchange("p", "product", "Output", 1.0),
                    raw_exchange("i", "feed", "Input", 2.0),
                    raw_exchange("e", "emission", "Output", 1.0),
                ],
            ),
            process(
                "bg",
                vec![
                    raw_exchange("p", "feed", "Output", 1.0),
                    raw_exchange("original", "emission", "Output", 999.0),
                ],
            ),
        ],
        model(json!([
            instance("a", "fg", json!([])),
            instance("b", "bg", json!([connection("feed", "a")]))
        ])),
        &["product", "feed", "emission"],
    );
    let mut local = local_input(source);
    local.backgrounds.push(background_inventory());
    local
}
fn compile(input: &ModelInput) -> anyhow::Result<PreparedInput> {
    local::prepare(&serde_json::to_vec(input)?, input, &methods())
}
#[test]
fn local_background_reuses_core_without_double_expansion_or_study_binding() {
    let input = foreground_background();
    let prepared = compile(&input).unwrap();
    assert_eq!(prepared.profile_id, local::PROFILE);
    assert!(prepared.study_binding.is_none());
    assert!(prepared.model.source_documents.is_empty());
    let result = local::execute(&prepared).unwrap();
    assert_eq!(result.g, vec![7.0]);
    assert_eq!(result.h, vec![14.0]);
    assert!(execute(&prepared).is_err());
    let bytes = artifact_bytes(&prepared).unwrap();
    assert!(decode_prepared(&bytes, &sha256(&bytes), &Bounds::default()).is_err());
    let mut doubled = input;
    doubled.amount = 2.0;
    assert_eq!(
        local::execute(&compile(&doubled).unwrap()).unwrap().g,
        vec![14.0]
    );
}
#[test]
fn local_snapshots_remain_editable_and_resource_budgets_can_exceed_study_defaults() {
    let mut input = local_input(single());
    input.amount = 3.0;
    input.limits.max_instances = 128;
    input.limits.max_views = 256;
    assert_eq!(
        local::execute(&compile(&input).unwrap()).unwrap().h,
        vec![30.0]
    );
    input.datasets[0]["processDataSet"]["exchanges"]["exchange"][1]["resultingAmount"] =
        json!(20.0);
    assert_eq!(
        local::execute(&compile(&input).unwrap()).unwrap().h,
        vec![60.0]
    );
    input.limits.max_memory_bytes = 1;
    assert!(compile(&input).is_err());
    input.limits.max_memory_bytes = 0;
    assert!(input.limits.bounds().is_err());
}
#[test]
fn local_inventory_rejects_incomplete_duplicate_or_ambiguous_data_and_missing_connections() {
    for change in 0..7 {
        let mut input = foreground_background();
        match change {
            0 => input.backgrounds[0]["complete"] = json!(false),
            1 => input.backgrounds[0]["data"]["nonzeroCount"] = json!(2),
            2 => {
                input.backgrounds[0]["data"]["result"]["quantitativeReference"]["value"] = json!(0);
            }
            3 => input.backgrounds[0]["data"]["result"]["uuid"] = json!("wrong"),
            4 => input.backgrounds.push(input.backgrounds[0].clone()),
            5 => {
                input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]["processes"]
                    ["processInstance"][1]["connections"]["outputExchange"] = json!([]);
            }
            _ => input.amount = f64::NAN,
        }
        assert!(compile(&input).is_err(), "case {change}");
    }
    let mut input = foreground_background();
    input.backgrounds[0]["data"]["items"][0]["amount"] = json!(-6.0);
    assert_eq!(
        local::execute(&compile(&input).unwrap()).unwrap().g,
        vec![-5.0]
    );
}

#[test]
fn complete_empty_inventory_is_zero_without_inventing_a_flow_axis() {
    let mut input = foreground_background();
    input.backgrounds[0]["data"]["items"] = json!([]);
    input.backgrounds[0]["data"]["nonzeroCount"] = json!(0);
    input.datasets[0]["processDataSet"]["exchanges"]["exchange"]
        .as_array_mut()
        .unwrap()
        .pop();
    let prepared = compile(&input).unwrap();
    assert!(prepared.numeric.inventory_axes.is_empty());
    let result = local::execute(&prepared).unwrap();
    assert!(result.g.is_empty());
    assert_eq!(result.h, vec![0.0]);
    assert!(result.complete_lcia_scores);
    input.backgrounds[0]["complete"] = json!(false);
    assert!(compile(&input).is_err());
}

#[test]
fn background_and_foreground_cannot_merge_incompatible_units_on_one_flow_axis() {
    let mut input = foreground_background();
    input.backgrounds[0]["data"]["items"][0]["key"]["unit"]["name"] = json!("g");
    assert!(
        compile(&input)
            .unwrap_err()
            .to_string()
            .contains("inventory_unit_mismatch")
    );
}

#[test]
fn multiple_explicit_background_instances_scale_independently() {
    let mut input = foreground_background();
    input.datasets[0]["processDataSet"]["exchanges"]["exchange"]
        .as_array_mut()
        .unwrap()
        .push(raw_exchange("j", "feed", "Input", 3.0));
    let instances = input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]
        ["processes"]["processInstance"]
        .as_array_mut()
        .unwrap();
    instances[1]["connections"]["outputExchange"][0]["downstreamProcess"]["@exchangeId"] =
        json!("i");
    let mut another = instance("c", "bg", json!([connection("feed", "a")]));
    another["connections"]["outputExchange"][0]["downstreamProcess"]["@exchangeId"] = json!("j");
    instances.push(another);
    assert_eq!(
        local::execute(&compile(&input).unwrap()).unwrap().g,
        vec![16.0]
    );
}

#[test]
fn local_instance_budget_can_run_beyond_the_hosted_default() {
    let mut input = foreground_background();
    let instances = input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]
        ["processes"]["processInstance"]
        .as_array_mut()
        .unwrap();
    for i in 0..64 {
        instances.push(instance(&format!("extra{i}"), "bg", json!([])));
    }
    assert!(compile(&input).is_err());
    input.limits.max_instances = 128;
    let prepared = compile(&input).unwrap();
    assert_eq!(prepared.model.instances.len(), 66);
    assert_eq!(local::execute(&prepared).unwrap().g, vec![7.0]);
}

#[test]
fn method_specific_absence_is_normal_and_does_not_make_local_scores_incomplete() {
    let input = foreground_background();
    let mut selected = methods();
    selected.factors[0].clear();
    let prepared = local::prepare(&serde_json::to_vec(&input).unwrap(), &input, &selected).unwrap();
    assert!(prepared.coverage.is_empty());
    let result = local::execute(&prepared).unwrap();
    assert_eq!(result.g, vec![7.0]);
    assert_eq!(result.h, vec![0.0]);
    assert!(result.complete_lcia_scores);
}
