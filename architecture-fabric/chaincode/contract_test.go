package main

import (
	"encoding/json"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/hyperledger/fabric-chaincode-go/v2/shim"
	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
	"github.com/hyperledger/fabric-protos-go-apiv2/ledger/queryresult"
	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type memoryStub struct {
	shim.ChaincodeStubInterface
	state     map[string][]byte
	txID      string
	timestamp *timestamppb.Timestamp
}

func newMemoryStub() *memoryStub {
	return &memoryStub{
		state: make(map[string][]byte),
		timestamp: timestamppb.New(
			time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		),
	}
}

func (stub *memoryStub) GetState(key string) ([]byte, error) {
	value := stub.state[key]
	return append([]byte(nil), value...), nil
}

func (stub *memoryStub) PutState(key string, value []byte) error {
	stub.state[key] = append([]byte(nil), value...)
	return nil
}

func (stub *memoryStub) DelState(key string) error {
	delete(stub.state, key)
	return nil
}

func (stub *memoryStub) GetTxID() string {
	return stub.txID
}

func (stub *memoryStub) GetTxTimestamp() (
	*timestamppb.Timestamp,
	error,
) {
	return stub.timestamp, nil
}

func (stub *memoryStub) CreateCompositeKey(
	objectType string,
	attributes []string,
) (string, error) {
	return compositeKey(objectType, attributes), nil
}

func (stub *memoryStub) GetStateByPartialCompositeKey(
	objectType string,
	attributes []string,
) (shim.StateQueryIteratorInterface, error) {
	prefix := compositeKey(objectType, attributes)
	keys := make([]string, 0)
	for key := range stub.state {
		if strings.HasPrefix(key, prefix) {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	values := make([]*queryresult.KV, 0, len(keys))
	for _, key := range keys {
		values = append(values, &queryresult.KV{
			Key:   key,
			Value: append([]byte(nil), stub.state[key]...),
		})
	}
	return &memoryIterator{values: values}, nil
}

type memoryIterator struct {
	values []*queryresult.KV
	index  int
}

func (iterator *memoryIterator) HasNext() bool {
	return iterator.index < len(iterator.values)
}

func (iterator *memoryIterator) Next() (*queryresult.KV, error) {
	value := iterator.values[iterator.index]
	iterator.index++
	return value, nil
}

func (iterator *memoryIterator) Close() error {
	return nil
}

func compositeKey(objectType string, attributes []string) string {
	return "\x00" + objectType + "\x00" +
		strings.Join(attributes, "\x00") + "\x00"
}

func testContext(stub *memoryStub, txID string) *contractapi.TransactionContext {
	stub.txID = txID
	context := &contractapi.TransactionContext{}
	context.SetStub(stub)
	return context
}

func TestContractAPITransactionSchemasAndJSONResponses(
	t *testing.T,
) {
	chaincode, err := contractapi.NewChaincode(
		&SmartContract{},
	)
	require.NoError(t, err)
	require.NotNil(t, chaincode)

	stub := newMemoryStub()
	contract := &SmartContract{}
	hash := "a8d9d9a49f61485af989fb47e7f5cdeaac028df03b6ae512d3d53c1b8a2b70bc"

	_, err = contract.CreateRecord(
		testContext(stub, "tx-schema-create"),
		"Patient",
		"patient-schema-check",
		hash,
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)

	accessPayload, err := contract.EvaluateAccess(
		testContext(stub, "tx-schema-access"),
		"Patient",
		"patient-schema-check",
		"clinician-schema",
		"org-002",
	)
	require.NoError(t, err)

	var access map[string]any
	require.NoError(
		t,
		json.Unmarshal(
			[]byte(accessPayload),
			&access,
		),
	)
	require.Equal(t, "DENY", access["decision"])

	matchedRuleID, exists := access["matchedRuleId"]
	require.True(t, exists)
	require.Nil(t, matchedRuleID)

	auditPayload, err := contract.GetAudit(
		testContext(stub, "tx-schema-audit"),
		"Patient",
		"patient-schema-check",
	)
	require.NoError(t, err)

	var events []map[string]any
	require.NoError(
		t,
		json.Unmarshal(
			[]byte(auditPayload),
			&events,
		),
	)
	require.Len(t, events, 2)
	require.Nil(t, events[0]["decision"])
	require.Equal(t, "DENY", events[1]["decision"])
}

func TestOP1ThroughOP6LedgerSemantics(t *testing.T) {
	stub := newMemoryStub()
	contract := &SmartContract{}
	hash := "a8d9d9a49f61485af989fb47e7f5cdeaac028df03b6ae512d3d53c1b8a2b70bc"

	evidence, err := contract.CreateRecord(
		testContext(stub, "tx-create"),
		"Patient",
		"patient-000001",
		hash,
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)
	require.Equal(t, hash, evidence.PayloadHash)

	defaultDecision, err := contract.evaluateAccess(
		testContext(stub, "tx-default"),
		"Patient",
		"patient-000001",
		"clinician-001",
		"org-002",
	)
	require.NoError(t, err)
	require.Equal(t, "DENY", defaultDecision.Decision)
	require.Nil(t, defaultDecision.MatchedRuleID)

	rule, err := contract.UpdateAuthorization(
		testContext(stub, "tx-rule"),
		"rule-allow-001",
		"Patient",
		"patient-000001",
		"clinician-001",
		"org-002",
		"ALLOW",
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)
	require.Equal(t, "ALLOW", rule.Decision)

	allowed, err := contract.evaluateAccess(
		testContext(stub, "tx-allowed"),
		"Patient",
		"patient-000001",
		"clinician-001",
		"org-002",
	)
	require.NoError(t, err)
	require.Equal(t, "ALLOW", allowed.Decision)
	require.NotNil(t, allowed.MatchedRuleID)
	require.Equal(t, "rule-allow-001", *allowed.MatchedRuleID)

	events, err := contract.getAuditEvents(
		testContext(stub, "tx-audit"),
		"Patient",
		"patient-000001",
	)
	require.NoError(t, err)
	require.Len(t, events, 4)
	require.Equal(t, []uint64{1, 2, 3, 4}, []uint64{
		events[0].Sequence,
		events[1].Sequence,
		events[2].Sequence,
		events[3].Sequence,
	})
	require.Equal(t, []string{"OP1", "OP4", "OP3", "OP4"}, []string{
		events[0].Action,
		events[1].Action,
		events[2].Action,
		events[3].Action,
	})
	require.Nil(t, events[0].Decision)
	require.Equal(t, "DENY", *events[1].Decision)
	require.Nil(t, events[2].Decision)
	require.Equal(t, "ALLOW", *events[3].Decision)
}

func TestStrictCreateAndMissingRecord(t *testing.T) {
	stub := newMemoryStub()
	contract := &SmartContract{}
	hash := "a8d9d9a49f61485af989fb47e7f5cdeaac028df03b6ae512d3d53c1b8a2b70bc"

	_, err := contract.CreateRecord(
		testContext(stub, "tx-create"),
		"Patient",
		"patient-000002",
		hash,
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)
	_, err = contract.CreateRecord(
		testContext(stub, "tx-duplicate"),
		"Patient",
		"patient-000002",
		hash,
		"administrator-001",
		"org-001",
	)
	require.ErrorContains(t, err, errRecordAlreadyExists)

	_, err = contract.GetRecordEvidence(
		testContext(stub, "tx-missing"),
		"Patient",
		"patient-missing",
	)
	require.ErrorContains(t, err, errRecordNotFound)
}

func TestAuthorizationUpsertReplacesRuleIdentifier(t *testing.T) {
	stub := newMemoryStub()
	contract := &SmartContract{}
	hash := "a8d9d9a49f61485af989fb47e7f5cdeaac028df03b6ae512d3d53c1b8a2b70bc"

	_, err := contract.CreateRecord(
		testContext(stub, "tx-create"),
		"Patient",
		"patient-000003",
		hash,
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)
	_, err = contract.UpdateAuthorization(
		testContext(stub, "tx-allow"),
		"rule-allow-003",
		"Patient",
		"patient-000003",
		"clinician-003",
		"org-002",
		"ALLOW",
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)
	_, err = contract.UpdateAuthorization(
		testContext(stub, "tx-deny"),
		"rule-deny-003",
		"Patient",
		"patient-000003",
		"clinician-003",
		"org-002",
		"DENY",
		"administrator-001",
		"org-001",
	)
	require.NoError(t, err)

	payload, err := contract.GetAccessDecision(
		testContext(stub, "tx-query"),
		"Patient",
		"patient-000003",
		"clinician-003",
		"org-002",
	)
	require.NoError(t, err)

	var result AccessResult
	require.NoError(
		t,
		json.Unmarshal([]byte(payload), &result),
	)
	require.Equal(t, "DENY", result.Decision)
	require.NotNil(t, result.MatchedRuleID)
	require.Equal(t, "rule-deny-003", *result.MatchedRuleID)
}
