import msal
import requests

# Replace these with your Azure AD and Power BI details
client_id = '1e477ddc-b90a-45c7-9f73-2ac3662de382'
client_secret = 'K468Q~uIMWOgV-xNGbAmkZCm9oX6BYef2suQNca.'
tenant_id = 'a0997b58-c1c2-4828-96a8-2b3276ab9dcc'
authority_url = f'https://login.microsoftonline.com/{tenant_id}'
scope = ['https://analysis.windows.net/powerbi/api/.default']

import Generic_Functions
import boto3
import os
from office365.sharepoint.client_context import ClientContext
from office365.runtime.auth.client_credential import ClientCredential
from office365.sharepoint.files.file import File

#Construct filename parameters based on current date
current_month = datetime.now().strftime('%m')
last_month = (datetime.now() - pd.DateOffset(months=2)).strftime('%m')
current_year = datetime.now().strftime('%Y')
DateStamp = current_year + '/' + current_month + '/' + current_year + current_month
last_day = str(
    Generic_Functions.days_in_month(int(current_year), int(current_month)))
DateStamp_lastmonth = current_year + '/' + last_month + '/' + current_year + last_month\
  + str(Generic_Functions.days_in_month(int(current_year), int(last_month)))

# S3 bucket and file details
Account_Balance_file = DateStamp + '-accountbalance-TBUK.csv'
Accounts_file = DateStamp + '-Accounts-TBUK.csv'
RiskReport_file = DateStamp + '-RiskReport-TBUK.csv'
BookingData_file = DateStamp + '-BookingData-TBUK.csv'
BookingDataAll_file = DateStamp_lastmonth + '-BookingDataAll-TBUK.csv'

# AWS S3 Configuration
AWS_ACCESS_KEY = os.environ['AWS_Public']
AWS_SECRET_KEY = os.environ['AWS_Private']
S3_BUCKET_NAME = "produk-rdsextracts-438255373632"
S3_FILE_KEY = "path/to/your/file.txt"  # File key in S3
LOCAL_FILE_PATH = "/tmp/file.txt"  # Temporary local storage

# SharePoint Configuration
SHAREPOINT_SITE_URL = "https://yourtenant.sharepoint.com/sites/yoursite"
SHAREPOINT_CLIENT_ID = "your_client_id"
SHAREPOINT_CLIENT_SECRET = "your_client_secret"
SHAREPOINT_FOLDER = "Shared Documents/YourFolder"  # Destination folder

def download_from_s3():
    """Download file from S3 to local directory"""
    s3 = boto3.client("s3", aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
    s3.download_file(S3_BUCKET_NAME, S3_FILE_KEY, LOCAL_FILE_PATH)
    print(f"Downloaded {S3_FILE_KEY} from S3 to {LOCAL_FILE_PATH}")

def upload_to_sharepoint():
    """Upload file to SharePoint"""
    ctx = ClientContext(SHAREPOINT_SITE_URL).with_credentials(ClientCredential(SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET))

    target_folder = ctx.web.get_folder_by_server_relative_url(SHAREPOINT_FOLDER)
    with open(LOCAL_FILE_PATH, "rb") as file_content:
        target_file = target_folder.upload_file(os.path.basename(LOCAL_FILE_PATH), file_content)
        ctx.execute_query()
        print(f"Uploaded {LOCAL_FILE_PATH} to SharePoint at {SHAREPOINT_FOLDER}")

def main():
    download_from_s3()
    upload_to_sharepoint()

if __name__ == "__main__":
    main()