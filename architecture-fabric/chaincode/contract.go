package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"time"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

const (
	errRecordAlreadyExists = "HAC_RECORD_ALREADY_EXISTS"
	errRecordNotFound      = "HAC_RECORD_NOT_FOUND"
	errRuleIDConflict      = "HAC_RULE_ID_CONFLICT"
	errInvalidArgument     = "HAC_INVALID_ARGUMENT"
)

var (
	fhirIDPattern     = regexp.MustCompile(`^[A-Za-z0-9.-]+$`)
	identifierPattern = regexp.MustCompile(
		`^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`,
	)
	digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

type SmartContract struct {
	contractapi.Contract
}

type RecordEvidence struct {
	ResourceType     string `json:"resourceType"`
	ResourceID       string `json:"resourceId"`
	Algorithm        string `json:"algorithm"`
	Canonicalization string `json:"canonicalization"`
	PayloadHash      string `json:"payloadHash"`
	CreatedAt        string `json:"createdAt"`
}

type AuthorizationRule struct {
	RuleID                  string `json:"ruleId"`
	ResourceType            string `json:"resourceType"`
	ResourceID              string `json:"resourceId"`
	PrincipalActorID        string `json:"principalActorId"`
	PrincipalOrganizationID string `json:"principalOrganizationId"`
	Decision                string `json:"decision"`
	UpdatedAt               string `json:"updatedAt"`
}

type AccessResult struct {
	ResourceType   string  `json:"resourceType"`
	ResourceID     string  `json:"resourceId"`
	ActorID        string  `json:"actorId"`
	OrganizationID string  `json:"organizationId"`
	Decision       string  `json:"decision"`
	MatchedRuleID  *string `json:"matchedRuleId"`
}

type AuditEvent struct {
	EventID        string  `json:"eventId"`
	Sequence       uint64  `json:"sequence"`
	ResourceType   string  `json:"resourceType"`
	ResourceID     string  `json:"resourceId"`
	ActorID        string  `json:"actorId"`
	OrganizationID string  `json:"organizationId"`
	Action         string  `json:"action"`
	Decision       *string `json:"decision"`
	Timestamp      string  `json:"timestamp"`
}

func (contract *SmartContract) CreateRecord(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	payloadHash string,
	actorID string,
	organizationID string,
) (*RecordEvidence, error) {
	if err := validateRecord(resourceType, resourceID); err != nil {
		return nil, err
	}
	if !digestPattern.MatchString(payloadHash) {
		return nil, invalidArgument("payloadHash")
	}
	if err := validateActor(actorID, organizationID); err != nil {
		return nil, err
	}

	key, err := recordKey(ctx, resourceType, resourceID)
	if err != nil {
		return nil, err
	}
	existing, err := ctx.GetStub().GetState(key)
	if err != nil {
		return nil, fmt.Errorf("read record evidence: %w", err)
	}
	if existing != nil {
		return nil, fmt.Errorf(
			"%s: record already exists: %s/%s",
			errRecordAlreadyExists,
			resourceType,
			resourceID,
		)
	}

	timestamp, err := transactionTimestamp(ctx)
	if err != nil {
		return nil, err
	}
	evidence := &RecordEvidence{
		ResourceType:     resourceType,
		ResourceID:       resourceID,
		Algorithm:        "sha256",
		Canonicalization: "RFC8785",
		PayloadHash:      payloadHash,
		CreatedAt:        timestamp,
	}
	if err := putJSON(ctx, key, evidence); err != nil {
		return nil, err
	}
	if _, err := contract.appendAudit(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
		"OP1",
		nil,
	); err != nil {
		return nil, err
	}
	return evidence, nil
}

func (contract *SmartContract) GetRecordEvidence(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) (*RecordEvidence, error) {
	if err := validateRecord(resourceType, resourceID); err != nil {
		return nil, err
	}
	key, err := recordKey(ctx, resourceType, resourceID)
	if err != nil {
		return nil, err
	}
	data, err := ctx.GetStub().GetState(key)
	if err != nil {
		return nil, fmt.Errorf("read record evidence: %w", err)
	}
	if data == nil {
		return nil, recordNotFound(resourceType, resourceID)
	}
	var evidence RecordEvidence
	if err := json.Unmarshal(data, &evidence); err != nil {
		return nil, fmt.Errorf("decode record evidence: %w", err)
	}
	return &evidence, nil
}

func (contract *SmartContract) UpdateAuthorization(
	ctx contractapi.TransactionContextInterface,
	ruleID string,
	resourceType string,
	resourceID string,
	principalActorID string,
	principalOrganizationID string,
	decision string,
	actorID string,
	organizationID string,
) (*AuthorizationRule, error) {
	if !identifierPattern.MatchString(ruleID) {
		return nil, invalidArgument("ruleId")
	}
	if err := validateRecord(resourceType, resourceID); err != nil {
		return nil, err
	}
	if err := validateActor(
		principalActorID,
		principalOrganizationID,
	); err != nil {
		return nil, err
	}
	if err := validateDecision(decision); err != nil {
		return nil, err
	}
	if err := validateActor(actorID, organizationID); err != nil {
		return nil, err
	}
	if err := requireRecord(ctx, resourceType, resourceID); err != nil {
		return nil, err
	}

	authorizationKey, err := authKey(
		ctx,
		resourceType,
		resourceID,
		principalActorID,
		principalOrganizationID,
	)
	if err != nil {
		return nil, err
	}
	ruleIndexKey, err := ruleIDKey(ctx, ruleID)
	if err != nil {
		return nil, err
	}
	indexedAuthorizationKey, err := ctx.GetStub().GetState(ruleIndexKey)
	if err != nil {
		return nil, fmt.Errorf("read rule identifier index: %w", err)
	}
	if indexedAuthorizationKey != nil &&
		string(indexedAuthorizationKey) != authorizationKey {
		return nil, fmt.Errorf(
			"%s: rule identifier is already used: %s",
			errRuleIDConflict,
			ruleID,
		)
	}

	existingData, err := ctx.GetStub().GetState(authorizationKey)
	if err != nil {
		return nil, fmt.Errorf("read authorization rule: %w", err)
	}
	if existingData != nil {
		var existing AuthorizationRule
		if err := json.Unmarshal(existingData, &existing); err != nil {
			return nil, fmt.Errorf(
				"decode authorization rule: %w",
				err,
			)
		}
		if existing.RuleID != ruleID {
			oldIndexKey, err := ruleIDKey(ctx, existing.RuleID)
			if err != nil {
				return nil, err
			}
			if err := ctx.GetStub().DelState(oldIndexKey); err != nil {
				return nil, fmt.Errorf(
					"remove previous rule identifier index: %w",
					err,
				)
			}
		}
	}

	timestamp, err := transactionTimestamp(ctx)
	if err != nil {
		return nil, err
	}
	rule := &AuthorizationRule{
		RuleID:                  ruleID,
		ResourceType:            resourceType,
		ResourceID:              resourceID,
		PrincipalActorID:        principalActorID,
		PrincipalOrganizationID: principalOrganizationID,
		Decision:                decision,
		UpdatedAt:               timestamp,
	}
	if err := putJSON(ctx, authorizationKey, rule); err != nil {
		return nil, err
	}
	if err := ctx.GetStub().PutState(
		ruleIndexKey,
		[]byte(authorizationKey),
	); err != nil {
		return nil, fmt.Errorf("write rule identifier index: %w", err)
	}
	if _, err := contract.appendAudit(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
		"OP3",
		nil,
	); err != nil {
		return nil, err
	}
	return rule, nil
}

func (contract *SmartContract) GetAccessDecision(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
) (string, error) {
	result, err := accessDecision(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
	)
	if err != nil {
		return "", err
	}
	return encodeJSON(result, "access decision")
}

func (contract *SmartContract) EvaluateAccess(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
) (string, error) {
	result, err := contract.evaluateAccess(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
	)
	if err != nil {
		return "", err
	}
	return encodeJSON(result, "access decision")
}

func (contract *SmartContract) evaluateAccess(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
) (*AccessResult, error) {
	if err := validateRecord(resourceType, resourceID); err != nil {
		return nil, err
	}
	if err := validateActor(actorID, organizationID); err != nil {
		return nil, err
	}
	if err := requireRecord(ctx, resourceType, resourceID); err != nil {
		return nil, err
	}

	result, err := accessDecision(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
	)
	if err != nil {
		return nil, err
	}

	if _, err := contract.appendAudit(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
		"OP4",
		&result.Decision,
	); err != nil {
		return nil, err
	}

	return result, nil
}

func (contract *SmartContract) GetAudit(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) (string, error) {
	events, err := contract.getAuditEvents(
		ctx,
		resourceType,
		resourceID,
	)
	if err != nil {
		return "", err
	}
	return encodeJSON(events, "audit events")
}

func (contract *SmartContract) getAuditEvents(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) ([]*AuditEvent, error) {
	if err := validateRecord(resourceType, resourceID); err != nil {
		return nil, err
	}
	if err := requireRecord(ctx, resourceType, resourceID); err != nil {
		return nil, err
	}
	iterator, err := ctx.GetStub().GetStateByPartialCompositeKey(
		"audit",
		[]string{resourceType, resourceID},
	)
	if err != nil {
		return nil, fmt.Errorf("query audit events: %w", err)
	}
	defer iterator.Close()

	events := make([]*AuditEvent, 0)
	for iterator.HasNext() {
		item, err := iterator.Next()
		if err != nil {
			return nil, fmt.Errorf("iterate audit events: %w", err)
		}
		var event AuditEvent
		if err := json.Unmarshal(item.Value, &event); err != nil {
			return nil, fmt.Errorf("decode audit event: %w", err)
		}
		events = append(events, &event)
	}
	return events, nil
}

func (contract *SmartContract) appendAudit(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
	action string,
	decision *string,
) (*AuditEvent, error) {
	sequenceKey, err := auditSequenceKey(
		ctx,
		resourceType,
		resourceID,
	)
	if err != nil {
		return nil, err
	}
	sequenceData, err := ctx.GetStub().GetState(sequenceKey)
	if err != nil {
		return nil, fmt.Errorf("read audit sequence: %w", err)
	}
	var sequence uint64 = 1
	if sequenceData != nil {
		current, err := strconv.ParseUint(
			string(sequenceData),
			10,
			64,
		)
		if err != nil {
			return nil, fmt.Errorf("decode audit sequence: %w", err)
		}
		sequence = current + 1
	}
	timestamp, err := transactionTimestamp(ctx)
	if err != nil {
		return nil, err
	}
	event := &AuditEvent{
		EventID:        "event-" + ctx.GetStub().GetTxID(),
		Sequence:       sequence,
		ResourceType:   resourceType,
		ResourceID:     resourceID,
		ActorID:        actorID,
		OrganizationID: organizationID,
		Action:         action,
		Decision:       decision,
		Timestamp:      timestamp,
	}
	eventKey, err := auditKey(
		ctx,
		resourceType,
		resourceID,
		sequence,
	)
	if err != nil {
		return nil, err
	}
	if err := putJSON(ctx, eventKey, event); err != nil {
		return nil, err
	}
	if err := ctx.GetStub().PutState(
		sequenceKey,
		[]byte(strconv.FormatUint(sequence, 10)),
	); err != nil {
		return nil, fmt.Errorf("write audit sequence: %w", err)
	}
	return event, nil
}

func accessDecision(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
) (*AccessResult, error) {
	key, err := authKey(
		ctx,
		resourceType,
		resourceID,
		actorID,
		organizationID,
	)
	if err != nil {
		return nil, err
	}
	data, err := ctx.GetStub().GetState(key)
	if err != nil {
		return nil, fmt.Errorf("read authorization rule: %w", err)
	}
	result := &AccessResult{
		ResourceType:   resourceType,
		ResourceID:     resourceID,
		ActorID:        actorID,
		OrganizationID: organizationID,
		Decision:       "DENY",
		MatchedRuleID:  nil,
	}
	if data == nil {
		return result, nil
	}
	var rule AuthorizationRule
	if err := json.Unmarshal(data, &rule); err != nil {
		return nil, fmt.Errorf("decode authorization rule: %w", err)
	}
	result.Decision = rule.Decision
	result.MatchedRuleID = &rule.RuleID
	return result, nil
}

func requireRecord(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) error {
	key, err := recordKey(ctx, resourceType, resourceID)
	if err != nil {
		return err
	}
	data, err := ctx.GetStub().GetState(key)
	if err != nil {
		return fmt.Errorf("read record evidence: %w", err)
	}
	if data == nil {
		return recordNotFound(resourceType, resourceID)
	}
	return nil
}

func recordKey(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) (string, error) {
	return ctx.GetStub().CreateCompositeKey(
		"record",
		[]string{resourceType, resourceID},
	)
}

func authKey(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	actorID string,
	organizationID string,
) (string, error) {
	return ctx.GetStub().CreateCompositeKey(
		"authorization",
		[]string{
			resourceType,
			resourceID,
			actorID,
			organizationID,
		},
	)
}

func ruleIDKey(
	ctx contractapi.TransactionContextInterface,
	ruleID string,
) (string, error) {
	return ctx.GetStub().CreateCompositeKey(
		"ruleid",
		[]string{ruleID},
	)
}

func auditSequenceKey(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
) (string, error) {
	return ctx.GetStub().CreateCompositeKey(
		"auditseq",
		[]string{resourceType, resourceID},
	)
}

func auditKey(
	ctx contractapi.TransactionContextInterface,
	resourceType string,
	resourceID string,
	sequence uint64,
) (string, error) {
	return ctx.GetStub().CreateCompositeKey(
		"audit",
		[]string{
			resourceType,
			resourceID,
			fmt.Sprintf("%020d", sequence),
		},
	)
}

func encodeJSON(value any, label string) (string, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return "", fmt.Errorf("encode %s: %w", label, err)
	}
	return string(data), nil
}

func putJSON(
	ctx contractapi.TransactionContextInterface,
	key string,
	value any,
) error {
	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("encode ledger value: %w", err)
	}
	if err := ctx.GetStub().PutState(key, data); err != nil {
		return fmt.Errorf("write ledger value: %w", err)
	}
	return nil
}

func transactionTimestamp(
	ctx contractapi.TransactionContextInterface,
) (string, error) {
	value, err := ctx.GetStub().GetTxTimestamp()
	if err != nil {
		return "", fmt.Errorf("read transaction timestamp: %w", err)
	}
	return value.AsTime().UTC().Format(time.RFC3339Nano), nil
}

func validateRecord(resourceType string, resourceID string) error {
	switch resourceType {
	case "Patient", "Observation", "Condition", "DiagnosticReport":
	default:
		return invalidArgument("resourceType")
	}
	if len(resourceID) < 1 ||
		len(resourceID) > 64 ||
		!fhirIDPattern.MatchString(resourceID) {
		return invalidArgument("resourceId")
	}
	return nil
}

func validateActor(actorID string, organizationID string) error {
	if !identifierPattern.MatchString(actorID) {
		return invalidArgument("actorId")
	}
	if !identifierPattern.MatchString(organizationID) {
		return invalidArgument("organizationId")
	}
	return nil
}

func validateDecision(decision string) error {
	if decision != "ALLOW" && decision != "DENY" {
		return invalidArgument("decision")
	}
	return nil
}

func invalidArgument(field string) error {
	return fmt.Errorf(
		"%s: invalid %s",
		errInvalidArgument,
		field,
	)
}

func recordNotFound(resourceType string, resourceID string) error {
	return fmt.Errorf(
		"%s: record not found: %s/%s",
		errRecordNotFound,
		resourceType,
		resourceID,
	)
}
