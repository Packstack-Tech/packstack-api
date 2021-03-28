# Packstack API
Packstack API server for the mobile app


### Connect to local k8s database
- Locate Postgres pod name: `kubectl get pods`
- Forward port: `kubectl port-forward pod/postgres-pod-name 5432:5432`
- Access database through `localhost:5432`

