import csv
import os
import base64
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from azure.identity import DefaultAzureCredential
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.costmanagement import CostManagementClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
# Make sure you have the following packages installed:
# pip install azure-identity azure-mgmt-subscription azure-mgmt-costmanagement python-dateutil sendgrid

def get_last_three_full_months():
    """
    Calculates the start and end dates for the last three full calendar months.
    Returns a list of dictionaries, each containing month name, start date, and end date.
    """
    today = datetime.now()
    month_data = []
    for i in range(3, 0, -1):
        start_of_month = (today - relativedelta(months=i)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_of_month = (start_of_month + relativedelta(months=1)) - timedelta(seconds=1)
        month_name = start_of_month.strftime('%B %Y')
        start_date_str = start_of_month.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date_str = end_of_month.strftime('%Y-%m-%dT%H:%M:%SZ')
        month_data.append({
            "name": month_name,
            "start": start_date_str,
            "end": end_date_str
        })
    return month_data

def get_subscription_costs(cost_client, scope, start_date, end_date):
    """
    Fetches costs for a subscription within a specific date range.
    Returns the total cost in the currency returned by Azure (already in INR).
    """
    try:
        query_definition = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": start_date,
                "to": end_date
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {
                    "totalCost": {
                        "name": "PreTaxCost",
                        "function": "Sum"
                    }
                }
            }
        }
        query_result = cost_client.query.usage(scope=scope, parameters=query_definition)
        if query_result.rows and len(query_result.rows) > 0:
            cost = float(query_result.rows[0][0])
            return cost
        else:
            print(f"   No cost data found for period {start_date} to {end_date}")
            return 0.0
    except Exception as e:
        print(f"   Error fetching cost data: {e}")
        return 0.0

def generate_cost_report():
    """
    Generates the Azure cost report and returns the filename and summary data.
    """
    subscription_ids_str = os.getenv("SUBSCRIPTION_IDS")
    if not subscription_ids_str:
        print("Error: SUBSCRIPTION_IDS environment variable is not set.")
        return None, None
        
    target_subscription_ids = [sub.strip() for sub in subscription_ids_str.split(',') if sub.strip()]
    if not target_subscription_ids:
        print("Please add at least one subscription ID to the 'SUBSCRIPTION_IDS' environment variable.")
        return None, None

    print("Authenticating with Azure via Service Principal...")
    try:
        credential = DefaultAzureCredential()
        token = credential.get_token("https://management.azure.com/.default")
        print("Authentication successful.")
    except Exception as e:
        print(f"Authentication failed. Please ensure you have configured credentials. Error: {e}")
        return None, None

    subscription_client = SubscriptionClient(credential)
    cost_client = CostManagementClient(credential)
    months = get_last_three_full_months()
    
    print(f"\nGenerating cost report for the following subscriptions: {target_subscription_ids}")
    print(f"Reporting period: {months[0]['name']} to {months[-1]['name']}\n")
    report_data = []
    summary_data = {}

    for sub_id in target_subscription_ids:
        report_row = {'Subscription ID': sub_id}
        try:
            sub = subscription_client.subscriptions.get(subscription_id=sub_id)
            report_row['Subscription Name'] = sub.display_name
            print(f"-> Processing subscription: {sub.display_name} ({sub_id})")
        except Exception as e:
            report_row['Subscription Name'] = "N/A"
            print(f"-> Error fetching details for subscription ID: {sub_id}. Details: {e}")
            continue
        for month in months:
            try:
                scope = f"/subscriptions/{sub_id}"
                cost = get_subscription_costs(cost_client, scope, month['start'], month['end'])
                report_row[month['name']] = cost
                print(f"   Cost for {month['name']}: ₹{cost:.2f} INR")
                if month['name'] not in summary_data:
                    summary_data[month['name']] = 0
                summary_data[month['name']] += cost
            except Exception as e:
                print(f"   Error fetching cost for {month['name']}. Details: {e}")
                report_row[month['name']] = 'N/A'
        report_data.append(report_row)

    file_name = f"azure_cost_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        fieldnames = ['Subscription ID', 'Subscription Name'] + [m['name'] for m in months]
        with open(file_name, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(report_data)
        print(f"\nCost report successfully saved to {file_name}")
        print("\nSummary:")
        for month_name, total_cost in summary_data.items():
            print(f"Total for {month_name}: ₹{total_cost:.2f} INR")
        return file_name, summary_data
    except PermissionError:
        print(f"\nPermission Denied: Could not write to '{file_name}'.")
        print("Please ensure the file is not open in another program (like Excel) and that you have write permissions for this directory.")
        return None, None
    except Exception as e:
        print(f"\nAn unexpected error occurred while writing the file: {e}")
        return None, None

def send_email_with_attachment(csv_file_path, summary_data):
    """
    Sends the cost report via SendGrid with the CSV file attached.
    """
    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    sender_email = os.getenv("SENDER_EMAIL")
    receiver_emails_str = os.getenv("RECEIVER_EMAILS")
    
    if not sendgrid_api_key or not sender_email or not receiver_emails_str:
        print("Error: Missing required SendGrid or email environment variables.")
        return False
        
    to_emails = [email.strip() for email in receiver_emails_str.split(',') if email.strip()]
    if not to_emails:
        print("Error: No receiver emails found in RECEIVER_EMAILS environment variable.")
        return False
    
    try:
        with open(csv_file_path, 'rb') as f:
            csv_data = f.read()
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return False
    
    subject = f"Azure Cost Report - {datetime.now().strftime('%B %Y')}"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #0078d4; color: white; padding: 20px; text-align: center; }}
            .content {{ padding: 20px; background-color: #f9f9f9; }}
            .footer {{ text-align: center; padding: 20px; font-size: 12px; color: #666; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Azure Cost Report</h1>
                <p>Pangea Production Environment</p>
            </div>
            
            <div class="content">
                <p>Dear IT Admin,</p>
                
                <p>Please find attached the Azure cost report for the last three months. This report provides a detailed breakdown of our cloud infrastructure costs across all the subscriptions.</p>
                
                <p>If you require any further details or have questions regarding this report, please contact the Production team.</p>
                
                <p>Best regards,<br>
                <strong>Platform Team</strong><br>
                Pangea Technologies</p>
            </div>
            
            <div class="footer">
                <p>This is an automated report. Please do not reply to this email.</p>
                <p>© {datetime.now().year} Pangea Technologies. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    text_content = f"""
Azure Cost Report - Pangea Production Environment
Dear IT Admin,
Please find attached the Azure cost report for the last three months providing a detailed cost breakdown across all subscriptions.
If you have questions or require further details, please contact the Platform team.
Best regards,
Platform Team
Pangea Technologies
"""

    try:
        message = Mail(
            from_email=sender_email,
            to_emails=to_emails,
            subject=subject,
            html_content=html_content,
            plain_text_content=text_content
        )
        encoded_file = base64.b64encode(csv_data).decode()
        attachment = Attachment(
            FileContent(encoded_file),
            FileName(os.path.basename(csv_file_path)),
            FileType('text/csv'),
            Disposition('attachment')
        )
        message.attachment = attachment
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        
        print(f"Email sent successfully! Status Code: {response.status_code}")
        return True
        
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def main():
    """
    Main function to generate the Azure cost report and send it via email.
    """
    print("Starting Azure Cost Report Generation...")
    print("=" * 50)
    
    csv_file, summary_data = generate_cost_report()
    
    if csv_file and summary_data:
        print("\n" + "=" * 50)
        print("Sending email with cost report...")
        success = send_email_with_attachment(csv_file, summary_data)
        if success:
            print("✅ Process completed successfully!")
            print(f"📊 Report generated: {csv_file}")
            print("📧 Email sent to IT Admin")
        else:
            print("❌ Failed to send email. Report was generated but not sent.")
    else:
        print("❌ Failed to generate cost report.")

if __name__ == "__main__":
    main()
