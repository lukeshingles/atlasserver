# Generated by Django 3.1.3 on 2020-11-02 14:08

from django.db import migrations, models
import forcephot.models


class Migration(migrations.Migration):

    dependencies = [
        ('forcephot', '0025_auto_20201102_1407'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='mjd_min',
            field=models.FloatField(blank=True, default=forcephot.models.get_mjd_min_default, null=True, verbose_name='MJD min'),
        ),
    ]
