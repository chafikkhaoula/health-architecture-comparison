package main

import (
	"log"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

func main() {
	chaincode, err := contractapi.NewChaincode(&SmartContract{})
	if err != nil {
		log.Panicf("create health-records chaincode: %v", err)
	}
	if err := chaincode.Start(); err != nil {
		log.Panicf("start health-records chaincode: %v", err)
	}
}
