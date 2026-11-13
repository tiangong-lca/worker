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
    assert_eq!(prepared.model.source_documents.len(), 0);
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
    assert_eq!(prepared.numeric.inventory_axes.len(), 0);
    let result = local::execute(&prepared).unwrap();
    assert_eq!(result.g.len(), 0);
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
    assert_eq!(prepared.coverage.len(), 0);
    let result = local::execute(&prepared).unwrap();
    assert_eq!(result.g, vec![7.0]);
    assert_eq!(result.h, vec![0.0]);
    assert!(result.complete_lcia_scores);
}

#[test]
fn background_reference_is_not_relabelled_as_another_product_or_direction() {
    for change in 0..5 {
        let mut input = foreground_background();
        let bg = &mut input.datasets[1]["processDataSet"];
        match change {
            0 => {
                bg["processInformation"]["quantitativeReference"]["referenceToReferenceFlow"] =
                    json!("original");
            }
            1 => bg["exchanges"]["exchange"][0]["referenceToFlowDataSet"]["@version"] = json!("2"),
            2 => bg["exchanges"]["exchange"][0]["exchangeDirection"] = json!("Input"),
            3 => {
                bg["processInformation"]["quantitativeReference"]["referenceToReferenceFlow"] =
                    json!("absent");
            }
            _ => {
                let duplicate = bg["exchanges"]["exchange"][0].clone();
                bg["exchanges"]["exchange"]
                    .as_array_mut()
                    .unwrap()
                    .push(duplicate);
            }
        }
        let error = compile(&input).unwrap_err().to_string();
        assert!(
            error.contains(if change < 3 {
                "local_background_reference_mismatch"
            } else {
                "local_background_reference_missing"
            }),
            "{error}"
        );
        assert!(error.contains("instance=b"), "{error}");
        if change < 3 {
            assert!(error.contains("actual_flow=feed"), "{error}");
        }
    }
}

fn proxy_input(ratio: f64, direct: f64) -> ModelInput {
    let mut input = foreground_background();
    let flow = input
        .datasets
        .iter()
        .find(|d| {
            d["flowDataSet"]["flowInformation"]["dataSetInformation"]["common:UUID"] == "feed"
        })
        .unwrap()
        .clone();
    let mut adapted = flow;
    adapted["flowDataSet"]["flowInformation"]["dataSetInformation"]["common:UUID"] =
        json!("adapted");
    input.datasets.push(adapted);
    input.datasets[0]["processDataSet"]["exchanges"]["exchange"][1]["referenceToFlowDataSet"] =
        reference("adapted");
    let proxy = process(
        "proxy",
        vec![
            raw_exchange("p", "adapted", "Output", 1.0),
            raw_exchange("i", "feed", "Input", ratio),
            raw_exchange("e", "emission", "Output", direct),
        ],
    );
    input
        .datasets
        .push(serde_json::from_str(&proxy.json).unwrap());
    let instances = &mut input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]
        ["processes"]["processInstance"];
    instances[1]["connections"]["outputExchange"] = json!([connection("feed", "c")]);
    instances.as_array_mut().unwrap().push(instance(
        "c",
        "proxy",
        json!([connection("adapted", "a")]),
    ));
    input
}

#[test]
fn ordinary_proxy_and_physical_conversion_preserve_upstream_burdens() {
    for (ratio, direct, expected) in [(1.0, 0.0, 7.0), (1.5, 0.0, 10.0), (1.5, 2.0, 14.0)] {
        let mut input = proxy_input(ratio, direct);
        // Foreground needs 2 adapted units and emits 1; background emits 6 per 2 feed.
        // Independent inventory: 1 + 2 * (ratio * 3 + direct).
        for demand in [1.0, 3.0] {
            input.amount = demand;
            let prepared = compile(&input).unwrap();
            let result = local::execute(&prepared).unwrap();
            assert_eq!(prepared.numeric.views.len(), 3);
            assert_eq!(result.g, vec![expected * demand]);
            assert_eq!(result.h, vec![2.0 * expected * demand]);
        }
    }
}

#[test]
fn proxy_does_not_relax_exact_connection_ports_or_units() {
    for change in 0..3 {
        let mut input = proxy_input(1.0, 0.0);
        match change {
            0 => {
                input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]["processes"]
                    ["processInstance"][1]["connections"]["outputExchange"][0]["@version"] =
                    json!("2");
            }
            1 => {
                input.datasets[1]["processDataSet"]["exchanges"]["exchange"][0]["exchangeDirection"] =
                    json!("Input");
            }
            _ => {
                input.backgrounds[0]["data"]["result"]["quantitativeReference"]["unit"]["name"] =
                    json!("g");
            }
        }
        let error = compile(&input).unwrap_err().to_string();
        if change == 0 {
            assert!(
                error.contains("instance=b flow=feed version=2 direction=Output matches=0"),
                "{error}"
            );
        } else if change == 1 {
            assert!(
                error.contains("local_background_reference_mismatch"),
                "{error}"
            );
        } else {
            assert!(
                error.contains("study_connection_identity_mismatch"),
                "{error}"
            );
        }
    }
}

fn signed_background(
    supplier_direction: &str,
    supplier_sign: f64,
    consumer_direction: &str,
    consumer_amount: f64,
    aggregate: bool,
) -> ModelInput {
    let mut input = foreground_background();
    if supplier_direction == "Input" {
        let flow = input
            .datasets
            .iter_mut()
            .find(|d| {
                d["flowDataSet"]["flowInformation"]["dataSetInformation"]["common:UUID"] == "feed"
            })
            .unwrap();
        flow["flowDataSet"]["modellingAndValidation"]["LCIMethod"]["typeOfDataSet"] =
            json!("Waste flow");
    }
    let fg = &mut input.datasets[0]["processDataSet"]["exchanges"]["exchange"][1];
    fg["exchangeDirection"] = json!(consumer_direction);
    fg["resultingAmount"] = json!(consumer_amount);
    let bg = &mut input.datasets[1]["processDataSet"]["exchanges"]["exchange"];
    bg[0]["exchangeDirection"] = json!(supplier_direction);
    bg[0]["resultingAmount"] = json!(supplier_sign);
    bg[1]["resultingAmount"] = json!(3.0);
    let reference = &mut input.backgrounds[0]["data"]["result"]["quantitativeReference"];
    reference["direction"] = json!(supplier_direction);
    reference["value"] = json!(supplier_sign * 2.0);
    let edge = &mut input.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]
        ["processes"]["processInstance"][1]["connections"]["outputExchange"][0];
    // Explicit ports permit treatment inputs and signed outputs; field names do not imply signs.
    edge["@exchangeId"] = json!("p");
    edge["downstreamProcess"]["@exchangeId"] = json!("i");
    if !aggregate {
        input.backgrounds.clear();
    }
    input
}

#[test]
fn signed_connections_balance_supply_treatment_and_negative_quantities() {
    for aggregate in [false, true] {
        for (sd, sa, cd, ca) in [
            ("Output", 1.0, "Input", 2.0),
            ("Input", 1.0, "Output", 2.0),
            ("Output", -1.0, "Output", 2.0),
            ("Input", -1.0, "Input", 2.0),
            ("Input", 1.0, "Input", -2.0),
            ("Output", 1.0, "Output", -2.0),
        ] {
            let mut input = signed_background(sd, sa, cd, ca, aggregate);
            for demand in [1.0, 3.0] {
                input.amount = demand;
                let result = local::execute(&compile(&input).unwrap()).unwrap();
                // 1 direct + abs(2) * 3 upstream; negative pivots do not invert burdens.
                assert_eq!(result.g, vec![7.0 * demand]);
                assert_eq!(result.h, vec![14.0 * demand]);
                assert!(result.x.iter().all(|v| *v >= 0.0));
            }
        }
    }
}

#[test]
fn same_signed_ports_explain_both_sides_without_rewriting_amounts() {
    for aggregate in [false, true] {
        for (sd, sa, cd, ca) in [
            ("Output", 1.0, "Output", 2.0),
            ("Input", 1.0, "Input", 2.0),
            ("Output", -1.0, "Input", 2.0),
            ("Input", -1.0, "Output", 2.0),
        ] {
            let input = signed_background(sd, sa, cd, ca, aggregate);
            let original = serde_json::to_value(&input).unwrap();
            let error = compile(&input).unwrap_err().to_string();
            assert!(
                error.starts_with("study_connection_direction_mismatch flow=feed version=1"),
                "{error}"
            );
            assert!(error.contains(&format!("supplier_instance=b supplier_exchange=p supplier_direction={sd} supplier_amount=")), "{error}");
            assert!(error.contains(&format!("consumer_instance=a consumer_exchange=i consumer_direction={cd} consumer_amount={ca:e} consumer_coefficient=")), "{error}");
            assert!(error.contains("supplier_coefficient="), "{error}");
            assert_eq!(serde_json::to_value(&input).unwrap(), original);
        }
    }
    // A zero demand has no sign to balance; a zero reference is still invalid.
    let zero = signed_background("Output", 1.0, "Input", 0.0, true);
    assert_eq!(
        local::execute(&compile(&zero).unwrap()).unwrap().g,
        vec![1.0]
    );
    assert!(compile(&signed_background("Output", 0.0, "Input", 2.0, true)).is_err());
}
