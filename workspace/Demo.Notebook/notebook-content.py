# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "11111111-2222-3333-4444-555555555555",
# META       "default_lakehouse_name": "SalesLakehouse",
# META       "default_lakehouse_workspace_id": "33333333-4444-5555-6666-777777777777"
# META     }
# META   }
# META }

# CELL ********************

# Placeholder — replace with your actual analyst-committed notebook content.
# The default_lakehouse GUID above is parameterized in parameter.yml (find_replace,
# item_type: ["Notebook", "DataPipeline"]) so it resolves to the correct Lakehouse
# per environment at deploy time.

df = spark.read.table("SalesLakehouse.sales_fact")
display(df.limit(10))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
