package main

import (
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-gateway/pkg/hash"
	"github.com/hyperledger/fabric-gateway/pkg/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type config struct {
	listenAddress       string
	mspID               string
	channelName         string
	chaincodeName       string
	peerEndpoint        string
	peerHostAlias       string
	tlsCertPath         string
	certificatePath     string
	privateKeyPath      string
	evaluateTimeout     time.Duration
	endorseTimeout      time.Duration
	submitTimeout       time.Duration
	commitStatusTimeout time.Duration
}

type server struct {
	contract *client.Contract
}

type transactionRequest struct {
	Function               string   `json:"function"`
	Arguments              []string `json:"arguments"`
	EndorsingOrganizations []string `json:"endorsing_organizations"`
}

type successResponse struct {
	Result         json.RawMessage `json:"result"`
	TransactionID  string          `json:"transaction_id,omitempty"`
	CommitStatus   string          `json:"commit_status,omitempty"`
	ValidationCode int32           `json:"validation_code"`
	BlockNumber    uint64          `json:"block_number"`
}

type errorEnvelope struct {
	Error errorResponse `json:"error"`
}

type errorResponse struct {
	Code           string `json:"code"`
	Message        string `json:"message"`
	Stage          string `json:"stage"`
	TransactionID  string `json:"transaction_id,omitempty"`
	ValidationCode int32  `json:"validation_code,omitempty"`
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		log.Fatal(err)
	}
	connection, err := newGRPCConnection(cfg)
	if err != nil {
		log.Fatalf("create gRPC connection: %v", err)
	}
	defer connection.Close()

	id, err := newIdentity(cfg)
	if err != nil {
		log.Fatalf("load client identity: %v", err)
	}
	sign, err := newSign(cfg)
	if err != nil {
		log.Fatalf("load client private key: %v", err)
	}
	gateway, err := client.Connect(
		id,
		client.WithSign(sign),
		client.WithHash(hash.SHA256),
		client.WithClientConnection(connection),
		client.WithEvaluateTimeout(cfg.evaluateTimeout),
		client.WithEndorseTimeout(cfg.endorseTimeout),
		client.WithSubmitTimeout(cfg.submitTimeout),
		client.WithCommitStatusTimeout(cfg.commitStatusTimeout),
	)
	if err != nil {
		log.Fatalf("connect Fabric Gateway: %v", err)
	}
	defer gateway.Close()

	network := gateway.GetNetwork(cfg.channelName)
	bridge := &server{
		contract: network.GetContract(cfg.chaincodeName),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", bridge.health)
	mux.HandleFunc("POST /v1/evaluate", bridge.evaluate)
	mux.HandleFunc("POST /v1/endorse", bridge.endorse)
	mux.HandleFunc("POST /v1/submit", bridge.submit)

	httpServer := &http.Server{
		Addr:              cfg.listenAddress,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      cfg.commitStatusTimeout + 5*time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}
	log.Printf(
		"Fabric Gateway bridge listening on %s for %s/%s",
		cfg.listenAddress,
		cfg.channelName,
		cfg.chaincodeName,
	)
	if err := httpServer.ListenAndServe(); !errors.Is(
		err,
		http.ErrServerClosed,
	) {
		log.Fatal(err)
	}
}

func (server *server) health(
	writer http.ResponseWriter,
	request *http.Request,
) {
	writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
}

func (server *server) evaluate(
	writer http.ResponseWriter,
	request *http.Request,
) {
	input, ok := decodeRequest(writer, request)
	if !ok {
		return
	}
	options := proposalOptions(input)
	proposal, err := server.contract.NewProposal(
		input.Function,
		options...,
	)
	if err != nil {
		writeGatewayError(writer, "proposal", "", err)
		return
	}
	result, err := proposal.Evaluate()
	if err != nil {
		writeGatewayError(
			writer,
			"evaluate",
			proposal.TransactionID(),
			err,
		)
		return
	}
	writeJSON(writer, http.StatusOK, successResponse{
		Result:        normalizedResult(result),
		TransactionID: proposal.TransactionID(),
	})
}

func (server *server) endorse(
	writer http.ResponseWriter,
	request *http.Request,
) {
	input, ok := decodeRequest(writer, request)
	if !ok {
		return
	}
	options := proposalOptions(input)
	proposal, err := server.contract.NewProposal(
		input.Function,
		options...,
	)
	if err != nil {
		writeGatewayError(writer, "proposal", "", err)
		return
	}
	transactionID := proposal.TransactionID()
	transaction, err := proposal.Endorse()
	if err != nil {
		writeGatewayError(
			writer,
			"endorse",
			transactionID,
			err,
		)
		return
	}
	writeJSON(writer, http.StatusOK, successResponse{
		Result:        normalizedResult(transaction.Result()),
		TransactionID: transactionID,
	})
}

func (server *server) submit(
	writer http.ResponseWriter,
	request *http.Request,
) {
	input, ok := decodeRequest(writer, request)
	if !ok {
		return
	}
	options := proposalOptions(input)
	proposal, err := server.contract.NewProposal(
		input.Function,
		options...,
	)
	if err != nil {
		writeGatewayError(writer, "proposal", "", err)
		return
	}
	transactionID := proposal.TransactionID()
	transaction, err := proposal.Endorse()
	if err != nil {
		writeGatewayError(
			writer,
			"endorse",
			transactionID,
			err,
		)
		return
	}
	commit, err := transaction.Submit()
	if err != nil {
		writeGatewayError(
			writer,
			"submit",
			transactionID,
			err,
		)
		return
	}
	status, err := commit.Status()
	if err != nil {
		writeGatewayError(
			writer,
			"commit_status",
			transactionID,
			err,
		)
		return
	}
	if !status.Successful {
		writeJSON(writer, http.StatusConflict, errorEnvelope{
			Error: errorResponse{
				Code: "INVALID_COMMIT",
				Message: fmt.Sprintf(
					"transaction committed with validation code %s",
					status.Code.String(),
				),
				Stage:          "commit_validation",
				TransactionID:  status.TransactionID,
				ValidationCode: int32(status.Code),
			},
		})
		return
	}
	writeJSON(writer, http.StatusOK, successResponse{
		Result:         normalizedResult(transaction.Result()),
		TransactionID:  status.TransactionID,
		CommitStatus:   "VALID",
		ValidationCode: int32(status.Code),
		BlockNumber:    status.BlockNumber,
	})
}

func proposalOptions(
	input transactionRequest,
) []client.ProposalOption {
	options := []client.ProposalOption{
		client.WithArguments(input.Arguments...),
	}
	if len(input.EndorsingOrganizations) > 0 {
		options = append(
			options,
			client.WithEndorsingOrganizations(
				input.EndorsingOrganizations...,
			),
		)
	}
	return options
}

func decodeRequest(
	writer http.ResponseWriter,
	request *http.Request,
) (transactionRequest, bool) {
	request.Body = http.MaxBytesReader(
		writer,
		request.Body,
		1<<20,
	)
	decoder := json.NewDecoder(request.Body)
	decoder.DisallowUnknownFields()
	var input transactionRequest
	if err := decoder.Decode(&input); err != nil {
		writeJSON(writer, http.StatusBadRequest, errorEnvelope{
			Error: errorResponse{
				Code:    "INVALID_REQUEST",
				Message: err.Error(),
				Stage:   "http",
			},
		})
		return transactionRequest{}, false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeJSON(writer, http.StatusBadRequest, errorEnvelope{
			Error: errorResponse{
				Code:    "INVALID_REQUEST",
				Message: "request body must contain one JSON object",
				Stage:   "http",
			},
		})
		return transactionRequest{}, false
	}
	input.Function = strings.TrimSpace(input.Function)
	if input.Function == "" {
		writeJSON(writer, http.StatusBadRequest, errorEnvelope{
			Error: errorResponse{
				Code:    "INVALID_REQUEST",
				Message: "function is required",
				Stage:   "http",
			},
		})
		return transactionRequest{}, false
	}
	return input, true
}

func writeGatewayError(
	writer http.ResponseWriter,
	stage string,
	transactionID string,
	err error,
) {
	code, status := classifyError(err)
	writeJSON(writer, status, errorEnvelope{
		Error: errorResponse{
			Code:          code,
			Message:       err.Error(),
			Stage:         stage,
			TransactionID: transactionID,
		},
	})
}

func classifyError(err error) (string, int) {
	message := err.Error()
	switch {
	case strings.Contains(message, "HAC_RECORD_ALREADY_EXISTS"):
		return "RECORD_ALREADY_EXISTS", http.StatusConflict
	case strings.Contains(message, "HAC_RECORD_NOT_FOUND"):
		return "RECORD_NOT_FOUND", http.StatusNotFound
	case strings.Contains(message, "HAC_RULE_ID_CONFLICT"):
		return "RULE_ID_CONFLICT", http.StatusConflict
	case strings.Contains(message, "HAC_INVALID_ARGUMENT"):
		return "INVALID_ARGUMENT", http.StatusBadRequest
	default:
		return "GATEWAY_ERROR", http.StatusBadGateway
	}
}

func normalizedResult(result []byte) json.RawMessage {
	if len(result) == 0 {
		return json.RawMessage("null")
	}
	if json.Valid(result) {
		return json.RawMessage(result)
	}
	encoded, _ := json.Marshal(string(result))
	return json.RawMessage(encoded)
}

func writeJSON(writer http.ResponseWriter, status int, value any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.Header().Set("Cache-Control", "no-store")
	writer.WriteHeader(status)
	if err := json.NewEncoder(writer).Encode(value); err != nil {
		log.Printf("encode HTTP response: %v", err)
	}
}

func loadConfig() (config, error) {
	var cfg config
	cfg.listenAddress = envOrDefault(
		"GATEWAY_LISTEN_ADDRESS",
		"127.0.0.1:18081",
	)
	cfg.mspID = envOrDefault("FABRIC_MSP_ID", "Org1MSP")
	cfg.channelName = envOrDefault(
		"FABRIC_CHANNEL",
		"healthchannel",
	)
	cfg.chaincodeName = envOrDefault(
		"FABRIC_CHAINCODE",
		"healthrecords",
	)
	cfg.peerEndpoint = envOrDefault(
		"FABRIC_PEER_ENDPOINT",
		"127.0.0.1:27051",
	)
	cfg.peerHostAlias = envOrDefault(
		"FABRIC_PEER_HOST_ALIAS",
		"peer0.org1.hac.example.com",
	)
	cfg.tlsCertPath = os.Getenv("FABRIC_TLS_CERT_PATH")
	cfg.certificatePath = os.Getenv("FABRIC_CERT_PATH")
	cfg.privateKeyPath = os.Getenv("FABRIC_KEY_PATH")

	required := map[string]string{
		"FABRIC_TLS_CERT_PATH": cfg.tlsCertPath,
		"FABRIC_CERT_PATH":     cfg.certificatePath,
		"FABRIC_KEY_PATH":      cfg.privateKeyPath,
	}
	for name, value := range required {
		if value == "" {
			return config{}, fmt.Errorf("%s is required", name)
		}
	}

	var err error
	if cfg.evaluateTimeout, err = durationEnv(
		"FABRIC_EVALUATE_TIMEOUT",
		5*time.Second,
	); err != nil {
		return config{}, err
	}
	if cfg.endorseTimeout, err = durationEnv(
		"FABRIC_ENDORSE_TIMEOUT",
		30*time.Second,
	); err != nil {
		return config{}, err
	}
	if cfg.submitTimeout, err = durationEnv(
		"FABRIC_SUBMIT_TIMEOUT",
		10*time.Second,
	); err != nil {
		return config{}, err
	}
	if cfg.commitStatusTimeout, err = durationEnv(
		"FABRIC_COMMIT_STATUS_TIMEOUT",
		60*time.Second,
	); err != nil {
		return config{}, err
	}
	return cfg, nil
}

func newGRPCConnection(cfg config) (*grpc.ClientConn, error) {
	certificatePEM, err := os.ReadFile(cfg.tlsCertPath)
	if err != nil {
		return nil, fmt.Errorf("read TLS certificate: %w", err)
	}
	certificate, err := identity.CertificateFromPEM(certificatePEM)
	if err != nil {
		return nil, fmt.Errorf("parse TLS certificate: %w", err)
	}
	certPool := x509.NewCertPool()
	certPool.AddCert(certificate)
	transportCredentials := credentials.NewClientTLSFromCert(
		certPool,
		cfg.peerHostAlias,
	)
	connection, err := grpc.NewClient(
		cfg.peerEndpoint,
		grpc.WithTransportCredentials(transportCredentials),
	)
	if err != nil {
		return nil, err
	}
	return connection, nil
}

func newIdentity(cfg config) (*identity.X509Identity, error) {
	certificatePEM, err := readFirstFile(cfg.certificatePath)
	if err != nil {
		return nil, err
	}
	certificate, err := identity.CertificateFromPEM(certificatePEM)
	if err != nil {
		return nil, err
	}
	return identity.NewX509Identity(cfg.mspID, certificate)
}

func newSign(cfg config) (identity.Sign, error) {
	privateKeyPEM, err := readFirstFile(cfg.privateKeyPath)
	if err != nil {
		return nil, err
	}
	privateKey, err := identity.PrivateKeyFromPEM(privateKeyPEM)
	if err != nil {
		return nil, err
	}
	return identity.NewPrivateKeySign(privateKey)
}

func readFirstFile(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return os.ReadFile(path)
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		return os.ReadFile(filepath.Join(path, entry.Name()))
	}
	return nil, fmt.Errorf("no files found in %s", path)
}

func envOrDefault(name string, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func durationEnv(
	name string,
	fallback time.Duration,
) (time.Duration, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	duration, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", name, err)
	}
	if duration <= 0 {
		return 0, fmt.Errorf("%s must be positive", name)
	}
	return duration, nil
}
